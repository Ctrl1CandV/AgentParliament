"""
runner.py——AgentParliament的核心执行层
职责：
1. 读取profiles.json中的模型与角色失败链配置
2. 以独立环境变量启动claude -p子进程，让同一个Claude Code CLI
   通过ANTHROPIC_BASE_URL、ANTHROPIC_AUTH_TOKEN和ANTHROPIC_MODEL
   扮演不同的模型DeepSeek和GLM等
3. 子进程统一以只读plan模式运行，不具备更改文件的能力，输出JSON后解析最终结果
4. 失败时按角色链自动降级到下一个模型
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import shutil
import json
import os

PROFILES_PATH = Path(__file__).with_name("profiles.json")
_DEBUG = os.environ.get("AGENTPARLIAMENT_DEBUG") == "1"
_DEBUG_LOG = Path(__file__).with_name("logs") / "debug.log"

class ProfileError(Exception):
    """ 配置缺失或非法时抛出 """

@dataclass
class ModelProfile:
    name: str
    base_url: str
    token: str
    model: str

    def build_env(self, base_env: dict[str, str]) -> dict[str, str]:
        """
        在基础环境之上叠加该模型的鉴权与路由变量
        注：复制一份再修改，避免污染父进程或其他子进程的环境
        """
        env = dict(base_env)
        env["ANTHROPIC_BASE_URL"] = self.base_url
        env["ANTHROPIC_AUTH_TOKEN"] = self.token
        env["ANTHROPIC_MODEL"] = self.model

        # 防止子CLI触发自动更新或遥测，保证headless行为稳定
        env["DISABLE_AUTOUPDATER"] = "1"
        env["DISABLE_TELEMETRY"] = "1"
        return env

@dataclass
class Config:
    timeout_seconds: int
    models: dict[str, ModelProfile]
    roles: dict[str, list[str]]

@dataclass
class AttemptResult:
    """ 单次子进程调用的结果记录，便于向上层透明地报告降级过程 """
    model_name: str
    ok: bool
    text: str = ""
    error: str = ""
    cost_usd: float | None = None

@dataclass
class RunResult:
    """ 一条失败链跑完后的最终结果 """
    ok: bool
    text: str
    model_used: str | None
    attempts: list[AttemptResult] = field(default_factory=list)

    @property
    def degraded(self) -> bool:
        """ 是否发生了降级，即不是链上第一个模型产出的结果 """
        return self.ok and len(self.attempts) > 1

    @property
    def total_cost_usd(self) -> float:
        """ 本轮所有尝试的累计成本 """
        return sum(a.cost_usd or 0.0 for a in self.attempts)

def load_config(path: Path = PROFILES_PATH) -> Config:
    """ 读取并校验profiles.json """
    if not path.exists():
        raise ProfileError(f"找不到配置文件：{path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"profiles.json不是合法JSON：{exc}") from exc

    defaults = raw.get("defaults", {})
    timeout_seconds = int(defaults.get("timeout_seconds", 300))

    raw_models = raw.get("models", {})
    if not raw_models:
        raise ProfileError("profiles.json的models为空，至少需要配置一个模型")

    models: dict[str, ModelProfile] = {}
    for name, item in raw_models.items():
        for key in ("base_url", "token", "model"):
            if not item.get(key):
                raise ProfileError(f"模型'{name}'缺少必填字段'{key}'")
        models[name] = ModelProfile(
            name=name,
            base_url=item["base_url"],
            token=item["token"],
            model=item["model"],
        )

    roles = raw.get("roles", {})
    if not roles:
        raise ProfileError("profiles.json的roles为空，至少需要配置一个角色")

    # 校验每个角色链里引用的模型都存在，避免运行时才报错。
    for role, chain in roles.items():
        if not chain:
            raise ProfileError(f"角色 '{role}' 的失败链为空。")
        for model_name in chain:
            if model_name not in models:
                raise ProfileError(
                    f"角色 '{role}' 引用了未定义的模型 '{model_name}'。"
                )

    return Config(
        timeout_seconds=timeout_seconds,
        models=models,
        roles=roles,
    )

def _debug_log(message: str) -> None:
    """ 仅在_DEBUG开启时写盘；任何写入异常都吞掉，绝不影响主流程 """
    if not _DEBUG:
        return
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a", encoding="utf-8") as fh:
            fh.write(message + "\n")
    except Exception:
        pass

def _resolve_claude_executable() -> str:
    """ 定位claude可执行文件，找不到时给出清晰报错 """
    exe = shutil.which("claude")
    if not exe:
        raise ProfileError("未在PATH中找到claude可执行文件，请确认已安装Claude Code CLI")
    return exe

def _parse_cli_json(stdout: str | None) -> tuple[bool, str, str, float | None]:
    """
    解析claude -p --output-format json的输出
    返回(ok, text, error, cost_usd)
    成功时取result字段为最终文本；is_error为真或字段缺失时视为失败
    同时提取total_cost_usd字段用于成本追踪
    """
    # 子进程异常退出或解码失败时subprocess可能把stdout置为None，先兜底
    if not stdout:
        return False, "", "子进程没有任何输出", None

    stdout = stdout.strip()
    if not stdout:
        return False, "", "子进程没有任何输出。", None

    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return False, "", f"无法解析CLI输出为JSON：{exc}", None

    cost_usd = None
    if "total_cost_usd" in payload:
        try:
            cost_usd = float(payload["total_cost_usd"])
        except (TypeError, ValueError):
            pass

    if payload.get("is_error"):
        return False, "", payload.get("result", "CLI返回is_error=true"), cost_usd

    result_text = payload.get("result")
    if not result_text:
        return False, "", "CLI输出中缺少result字段或为空。", cost_usd

    return True, result_text, "", cost_usd

def _classify_error(returncode: int, stderr: str) -> str:
    """ 根据退出码和stderr关键词对失败原因做基本分类，方便降级日志快速定位 """
    stderr_lower = stderr.lower()
    if returncode == 1 and (
        "auth" in stderr_lower or "api key" in stderr_lower
        or "unauthorized" in stderr_lower or "401" in stderr_lower
        or "403" in stderr_lower
    ):
        return "鉴权失败"
    if "rate" in stderr_lower or "429" in stderr_lower or "quota" in stderr_lower:
        return "额度/限流"
    if "model" in stderr_lower and ("not found" in stderr_lower or "does not exist" in stderr_lower):
        return "模型ID无效"
    if returncode == 1 and (
        "connection" in stderr_lower or "timeout" in stderr_lower
        or "econnrefused" in stderr_lower or "enotfound" in stderr_lower
    ):
        return "网络连接"
    return "未知错误"


def _run_once(
    profile: ModelProfile,
    prompt: str, cwd: str,
    timeout_seconds: int,
    extra_dirs: list[str] | None = None,
) -> AttemptResult:
    """
    用指定模型profile启动一次只读子进程并返回结果
    只依赖--permission-mode plan保证只读：plan模式下Claude Code内置
    禁止所有写操作，无需再额外传 --allowedTools
    这样副Agent仍可使用Read/Grep/Glob以及只读Bash等内置工具完成调研
    """
    claude_exe = _resolve_claude_executable()

    cmd = [
        claude_exe,
        "--output-format", "json",
        "--permission-mode", "plan",
        "-p",
        prompt,
    ]
    for directory in extra_dirs or []:
        cmd += ["--add-dir", directory]

    env = profile.build_env(os.environ)
    try:
        """
        Windows上text=True默认按系统ANSI编码解码
        claude CLI输出包含UTF-8字符时会在subprocess的内部读线程触发UnicodeDecodeError，导致stdout为None
        这里强制按UTF-8解码，并对极少数无法解码的字节用replace兜底，既不丢失绝大多数输出，也不会让父进程崩溃
        """
        completed = subprocess.run(
            cmd, cwd=cwd, env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return AttemptResult(
            model_name=profile.name, ok=False,
            error=f"子进程超时>{timeout_seconds}s",
        )

    if _DEBUG:
        # 落盘子进程真实结果与可能干扰claude的宿主环境变量，便于排查环境差异
        suspect_keys = [
            "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL",
            "CLAUDE_CONFIG_DIR", "HTTP_PROXY", "HTTPS_PROXY",
        ]
        env_dump = {k: env.get(k) for k in suspect_keys if env.get(k) is not None}
        _debug_log(
            f"--- model={profile.name} cwd={cwd} ---\n"
            f"returncode={completed.returncode}\n"
            f"env(关键变量)={env_dump}\n"
            f"stdout[:800]={(completed.stdout or '')[:800]!r}\n"
            f"stderr[:800]={(completed.stderr or '')[:800]!r}\n"
        )

    if completed.returncode != 0:
        # 退出码非0，即出现鉴权失败、额度耗尽、进程中途挂掉等情况
        stderr = (completed.stderr or "").strip()

        error_type = _classify_error(completed.returncode, stderr)
        return AttemptResult(
            model_name=profile.name, ok=False,
            error=f"[{error_type}] 子进程退出码{completed.returncode}：{stderr[:500]}",
        )

    # 解析json格式
    ok, text, error, cost_usd = _parse_cli_json(completed.stdout)
    return AttemptResult(
        model_name=profile.name, ok=ok, text=text, error=error,
        cost_usd=cost_usd,
    )

def run_with_chain(
    config: Config, role: str,
    prompt: str, cwd: str,
    extra_dirs: list[str] | None = None,
) -> RunResult:
    """
    按角色失败链依次尝试，第一个成功即返回；全部失败则返回失败结果
    因为只读任务幂等，降级重跑是安全的
    """
    if role not in config.roles:
        raise ProfileError(f"未定义的角色'{role}'")

    attempts: list[AttemptResult] = []
    for model_name in config.roles[role]:
        profile = config.models[model_name]
        attempt = _run_once(
            profile=profile,
            prompt=prompt,
            cwd=cwd,
            timeout_seconds=config.timeout_seconds,
            extra_dirs=extra_dirs,
        )
        attempts.append(attempt)
        if attempt.ok:
            return RunResult(
                ok=True,
                text=attempt.text,
                model_used=model_name,
                attempts=attempts,
            )

    # 链上所有模型都失败了
    return RunResult(ok=False, text="", model_used=None, attempts=attempts)

@dataclass
class ParallelResult:
    """ 并行执行多个模型后的汇总结果 """
    results: list[AttemptResult]
    total_cost_usd: float = 0.0

    @property
    def successful(self) -> list[AttemptResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed(self) -> list[AttemptResult]:
        return [r for r in self.results if not r.ok]

    @property
    def all_failed(self) -> bool:
        return not self.successful

def run_parallel(
    config: Config, role: str,
    prompt: str, cwd: str,
    extra_dirs: list[str] | None = None,
) -> ParallelResult:
    """
    并行启动角色链上的所有模型，每个模型独立执行，互不降级
    适用场景：需要多个模型的独立视角，如独立分析、共识对比等，而不是"只要一个结果就行"的串行降级场景
    """
    if role not in config.roles:
        raise ProfileError(f"未定义的角色'{role}'")

    model_names = config.roles[role]
    results: list[AttemptResult] = [None] * len(model_names)

    def _run_at_index(index: int, model_name: str) -> tuple[int, AttemptResult]:
        try:
            profile = config.models[model_name]
            attempt = _run_once(
                profile=profile,
                prompt=prompt,
                cwd=cwd,
                timeout_seconds=config.timeout_seconds,
                extra_dirs=extra_dirs,
            )
        except Exception as exc:
            # 单个模型的意外异常不应拖垮整个并行批次，转成失败结果以保持run_parallel"各模型互不影响"的契约
            attempt = AttemptResult(
                model_name=model_name, ok=False,
                error=f"执行时发生未预期异常：{exc}",
            )
        return index, attempt

    with ThreadPoolExecutor(max_workers=len(model_names)) as executor:
        futures = {
            executor.submit(_run_at_index, i, name): i
            for i, name in enumerate(model_names)
        }
        for future in as_completed(futures):
            index, attempt = future.result()
            results[index] = attempt

    total_cost = sum(r.cost_usd or 0.0 for r in results)
    return ParallelResult(results=results, total_cost_usd=total_cost)

if __name__ == "__main__":
    # 冒烟测试：用researcher角色对当前目录跑一次只读调研
    import sys

    question = sys.argv[1] if len(sys.argv) > 1 else "这个项目的目录结构和用途是什么？"
    cfg = load_config()
    result = run_with_chain(
        config=cfg,
        role="researcher",
        prompt=question,
        cwd=str(Path(__file__).parent),
    )
    print("=== 是否成功 ===", result.ok)
    print("=== 使用模型 ===", result.model_used, "已降级" if result.degraded else "")
    print("=== 尝试记录 ===")
    for a in result.attempts:
        print(f"  - {a.model_name}: {'OK' if a.ok else 'FAIL'} {a.error}")
    print("=== 结果文本 ===")
    print(result.text or "(无)")
