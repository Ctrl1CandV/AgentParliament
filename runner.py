"""
runner.py——AgentParliament的核心执行层
职责：
1. 读取profiles.json中的模型与角色失败链配置
2. 以独立环境变量启动claude -p子进程，让同一个Claude Code CLI
   通过ANTHROPIC_BASE_URL、ANTHROPIC_AUTH_TOKEN和ANTHROPIC_MODEL
   扮演不同的模型，如DeepSeek和GLM等
3. 子进程统一以只读plan模式运行，不具备更改文件的能力，输出JSON后解析最终结果
4. 失败时按角色链自动降级到下一个模型
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
import subprocess
import tempfile
import shutil
import json
import os

PROFILES_PATH = Path(__file__).with_name("profiles.json")
_DEBUG = os.environ.get("AGENTPARLIAMENT_DEBUG") == "1"
_DEBUG_LOG = Path(__file__).with_name("logs") / "debug.log"

"""
每个model的独立CLAUDE_CONFIG_DIR根目录，放在临时目录避免污染用户~/.claude
关键作用：绕开用户~/.claude/settings.json中ccswitch写入的env块
该env块的优先级高于子进程继承的环境变量，会把我们设的ANTHROPIC_MODEL覆盖掉
"""
_ISOLATED_CONFIG_ROOT = Path(tempfile.gettempdir()) / "agent-parliament-cfg"

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

        # 关键隔离：为每个model分配独立的CLAUDE_CONFIG_DIR
        cfg_dir = _ISOLATED_CONFIG_ROOT / self.name
        cfg_dir.mkdir(parents=True, exist_ok=True)
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

        """
        清理父进程可能存在的、会干扰认证路由的变量
        ccswitch等工具可能注入任意ANTHROPIC_/CLAUDE_CODE_前缀的变量，逐一列举易遗漏
        这里统一清除这两个前缀的全部变量，再在下面注入我们自己需要的，从根上杜绝串扰
        注意：不清理CLAUDE_CONFIG_DIR本身
        """
        prefixes_to_clean = ("ANTHROPIC_", "CLAUDE_CODE_")
        for key in [k for k in env if k.startswith(prefixes_to_clean)]:
            env.pop(key, None)

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
    """
    定位claude可执行文件，优先绕过.cmd/.ps1 shim
    Windows上shutil.which可能命中npm生成的claude.cmd/claude.ps1
    这类shim经cmd.exe解析%*时会在换行符处截断含多行的参数，导致prompt只剩第一行
    因此优先定位同目录下真实的claude.exe，使subprocess走CreateProcess而非cmd.exe，多行参数才能安全传递
    """
    raw = shutil.which("claude")
    if not raw:
        raise ProfileError("未在PATH中找到claude可执行文件，请确认已安装Claude Code CLI")

    if raw.lower().endswith((".cmd", ".ps1")):
        # 同目录下的同名.exe
        candidate = Path(raw).with_suffix(".exe")
        if candidate.exists():
            return str(candidate)
        # npm全局安装时真实exe在node_modules子目录下
        npm_exe = (
            Path(raw).parent
            / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        )
        if npm_exe.exists():
            return str(npm_exe)

    # 非Windows或已命中.exe，直接返回
    return raw

def _parse_cli_json(
    stdout: str | None,
    expected_prompt_chars: int = 0,
) -> tuple[bool, str, str, float | None]:
    """
    解析claude -p --output-format json的输出
    返回(ok, text, error, cost_usd)
    成功时取result字段为最终文本；is_error为真或字段缺失时视为失败
    同时提取total_cost_usd字段用于成本追踪
    expected_prompt_chars为本次传入prompt的字符数，用于护栏检测prompt是否被截断
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

    # 护栏：prompt预期较长但实际input_tokens极少，说明prompt可能在传参链路被截断
    # 中文约2字符/token，英文约4字符/token，取保守值3做估算；只在明显异常时告警，不阻断结果
    if expected_prompt_chars > 200:
        usage = payload.get("usage") or {}
        input_tokens = usage.get("input_tokens", 0)
        estimated_tokens = expected_prompt_chars / 3
        if 0 < input_tokens < estimated_tokens * 0.3:
            warning = (
                f"⚠️ 疑似prompt未完整送达：预期约{expected_prompt_chars}字符"
                f"（≈{estimated_tokens:.0f} tokens），实际input_tokens={input_tokens}，请检查传参链路。"
            )
            return True, f"{warning}\n\n{result_text}", "", cost_usd

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

    # 兜底：退出码非0但stderr为空，给出比"未知错误"更有用的排查方向
    if returncode != 0 and not stderr.strip():
        return (
            f"子进程静默失败（退出码{returncode}，stderr为空），"
            f"常见原因：环境变量串扰、模型ID无效、或prompt传参截断"
        )
    return "未知错误"


def _run_once(
    profile: ModelProfile,
    prompt: str, cwd: str,
    timeout_seconds: int,
    extra_dirs: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> AttemptResult:
    """
    用指定模型profile启动一次只读子进程并返回结果
    只依赖--permission-mode plan保证只读：plan模式下Claude Code内置
    禁止所有写操作，无需再额外传 --allowedTools 来保证只读
    这样副Agent仍可使用Read/Grep/Glob以及只读Bash等内置工具完成调研
    allowed_tools用于按需放开plan模式下默认不自动允许的只读工具（如WebSearch），
    它只做加法，不会突破plan模式的只读边界
    """
    claude_exe = _resolve_claude_executable()

    cmd = [
        claude_exe,
        "--output-format", "json",
        "--permission-mode", "plan",
    ]
    for directory in extra_dirs or []:
        cmd += ["--add-dir", directory]
    if allowed_tools:
        cmd += ["--allowedTools", ",".join(allowed_tools)]
    cmd.append("-p")

    env = profile.build_env(os.environ)
    try:
        completed = subprocess.run(
            cmd, cwd=cwd, env=env,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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

    # 解析json格式，传入prompt字符数用于截断护栏检测
    ok, text, error, cost_usd = _parse_cli_json(completed.stdout, len(prompt))
    return AttemptResult(
        model_name=profile.name, ok=ok, text=text, error=error,
        cost_usd=cost_usd,
    )

def run_with_chain(
    config: Config, role: str,
    prompt: str, cwd: str,
    extra_dirs: list[str] | None = None,
    allowed_tools: list[str] | None = None,
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
            allowed_tools=allowed_tools,
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
    allowed_tools: list[str] | None = None,
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
                allowed_tools=allowed_tools,
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

def health_check(config: Config) -> list[str]:
    """
    快速验证传参链路是否正常：用一条含多行的探针prompt跑一次，确认prompt完整送达
    返回警告列表，空列表表示一切正常
    注意：会真实调用一次模型（产生少量费用），因此不在正常工具调用路径上自动触发，仅供手动自检
    """
    warnings: list[str] = []
    # 取任意一条角色链的首个模型作为探针对象
    first_chain = next(iter(config.roles.values()))
    first_model = first_chain[0]
    profile = config.models[first_model]

    probe_prompt = "请原样返回以下三行内容，不要添加任何解释：\nPROBE_LINE1\nPROBE_LINE2\nPROBE_LINE3"
    result = _run_once(
        profile=profile,
        prompt=probe_prompt,
        cwd=str(Path(__file__).parent),
        timeout_seconds=min(config.timeout_seconds, 60),
    )
    if not result.ok:
        warnings.append(f"自检失败：探针模型'{first_model}'调用失败（{result.error}）")
    elif "PROBE_LINE3" not in result.text:
        warnings.append(
            f"自检失败：探针模型'{first_model}'未返回末行PROBE_LINE3，疑似prompt传参截断。"
            f"实际返回：{result.text[:200]}"
        )
    return warnings

if __name__ == "__main__":
    # 命令行用法：
    # python runner.py "调研问题"   -> 用researcher角色跑一次只读调研
    # python runner.py --selfcheck  -> 跑一次传参链路自检
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--selfcheck":
        cfg = load_config()
        issues = health_check(cfg)
        if issues:
            print("=== 自检发现问题 ===")
            for item in issues:
                print(f"  - {item}")
        else:
            print("=== 自检通过：传参链路正常 ===")
        sys.exit(1 if issues else 0)

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
