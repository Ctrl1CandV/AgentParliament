"""
worktree_manager.py——AgentParliament 的 git worktree 隔离执行环境管理

职责：
1. 创建基于临时分支的 git worktree，作为 Tier 2（隔离可写可执行）的承载环境
2. 提取子 Agent 在 worktree 内的所有改动，产出"建议性 diff"（不直接落盘主仓库）
3. 销毁 worktree + 临时分支，MCP 进程异常退出时由 atexit 钩子兜底清理

安全边界（必须在对外文档中记录）：
- worktree 隔离的是**文件树**，不是**操作系统进程**。
- 子 Agent 在 worktree 内执行的代码（如 pytest），理论上仍可访问 worktree 之外的文件系统、可联网。
- 这是威胁模型从"子 Agent 绝对不能写"变成"我们运行了一段不完全受控的代码"的实质性转变，
  是有意识的取舍。主 Agent 拿到的是"建议性 diff"，是否合并由主 Agent（最终由用户）决定。

设计参考：runner.py 的 _RUNNING_PROCS + atexit.register 模式，
用全局登记表跟踪活跃 worktree，进程退出时清理残留，避免孤儿 worktree 累积。
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import threading
import tempfile
import atexit
import uuid
import os

# 全局登记表：跟踪所有活跃的 worktree，进程异常退出时由 atexit 钩子清理
# key = worktree 路径（字符串），value = 对应的 WorktreeSession 实例
_WT_LOCK = threading.Lock()
_ACTIVE_WORKTREES: dict[str, WorktreeSession] = {}


class WorktreeError(Exception):
    """ worktree 创建/销毁等操作失败时抛出 """


class WorktreeSession:
    """
    单个 git worktree 的生命周期管理。

    生命周期：
        create() → 子 Agent 在 self.path 下工作 → get_diff() 提取改动 → destroy()

    隔离机制：
    - git worktree add 建出基于临时分支的独立工作区，复用真实仓库完整状态（依赖、import 关系完整）
    - 子 Agent 的改动只影响这个临时分支，主仓库的工作树保持干净
    - destroy() 时销毁 worktree 并删除临时分支，不留痕迹
    """

    def __init__(self, base_repo_dir: str):
        """
        Args:
            base_repo_dir: 主仓库根目录（git worktree add 的基准）
        """
        self.base_repo_dir = str(Path(base_repo_dir).resolve())
        # 临时分支名：ap-exp-<12位uuid>，避免与真实分支碰撞
        self.branch_name = f"ap-exp-{uuid.uuid4().hex[:12]}"
        # worktree 路径：放在系统临时目录下，路径含 branch 名避免并发碰撞
        self.path = str(Path(tempfile.gettempdir()) / "agent-parliament-wt" / self.branch_name)
        self._destroyed = False
        # 是否成功把主仓库"未提交改动"纳入了 worktree 快照（供诊断/结果标注）
        # False 表示回退到了基于 HEAD 的创建（只含已提交代码）
        self.snapshot_used = False

    def create(self) -> str:
        """
        创建 worktree，返回其路径。失败抛 WorktreeError。

        用 -b 建临时分支，使 worktree 的 HEAD 指向这个独立分支，
        子 Agent 的所有提交/改动都落在这个分支上，与主仓库隔离。

        关键：worktree 基于「包含主仓库未提交改动的快照 commit」创建，而非裸 HEAD。
        因为验证的往往是尚未 commit 的实现——若基于 HEAD，子 Agent 会对着旧代码跑测试，
        产出带可信度的错误 ground truth。快照通过独立临时索引生成，绝不触碰主仓库工作树与索引
        （详见 _create_snapshot_commit）。快照失败时回退到基于 HEAD 创建，并置 snapshot_used=False。
        """
        if self._destroyed:
            raise WorktreeError("worktree 已销毁，不能重复使用")

        # 确保父目录存在
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        # 尝试把主仓库未提交改动打成游离快照 commit；失败则回退到 HEAD
        snapshot_commit = self._create_snapshot_commit()
        base_ref = snapshot_commit or "HEAD"
        self.snapshot_used = bool(snapshot_commit)

        # git worktree add <path> -b <branch> <base_ref>
        # base_ref 为快照 commit 时，worktree 反映主仓库"当前真实代码状态"（含未提交改动）
        result = subprocess.run(
            ["git", "worktree", "add", self.path, "-b", self.branch_name, base_ref],
            cwd=self.base_repo_dir,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if result.returncode != 0:
            raise WorktreeError(
                f"git worktree add 失败（退出码{result.returncode}）：{result.stderr.strip()[:300]}"
            )

        # 登记到全局表，供 atexit 清理
        with _WT_LOCK:
            _ACTIVE_WORKTREES[self.path] = self

        return self.path

    def _create_snapshot_commit(self) -> str | None:
        """
        把主仓库工作区的未提交改动（含已暂存、未暂存、未跟踪）打成一个游离的快照 commit，
        返回 commit SHA；失败（非 git 仓库、无 HEAD、git 报错等）时返回 None，调用方回退到基于 HEAD 创建。

        ── 安全保证：全程绝不修改主仓库的工作树与 .git/index ──
        - 用一个独立的临时 GIT_INDEX_FILE，所有 read-tree/add/write-tree 只写这个临时索引
        - git add -A 只读取工作树文件内容（把 blob 写入 object db），不改动工作树本身
        - 产物是一个 dangling commit：不移动任何分支、不动 HEAD、不写主索引
        因此测试全程及测试后，主仓库代码与原先字节级一致。

        附带正确性：.gitignore 中的文件（如含 token 的 profiles.json）不会被纳入快照，
        既避免凭据泄漏进临时副本，也让 get_diff 只反映子 Agent 的新增改动（而非开发者原有的未提交改动）。
        """
        tmp_index = None
        try:
            # 独立临时索引文件：先创建再删除，交给 git read-tree 重建（0 字节文件不是合法索引）
            fd, tmp_index = tempfile.mkstemp(prefix="ap-wt-index-", suffix=".idx")
            os.close(fd)
            os.unlink(tmp_index)

            env = dict(os.environ)
            env["GIT_INDEX_FILE"] = tmp_index
            # 提供确定性身份，避免机器未配置 git 身份时 commit-tree 失败
            env.setdefault("GIT_AUTHOR_NAME", "agent-parliament")
            env.setdefault("GIT_AUTHOR_EMAIL", "ap@localhost")
            env.setdefault("GIT_COMMITTER_NAME", "agent-parliament")
            env.setdefault("GIT_COMMITTER_EMAIL", "ap@localhost")

            def _git(args: list[str]) -> subprocess.CompletedProcess:
                return subprocess.run(
                    ["git"] + args, cwd=self.base_repo_dir, env=env,
                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                    timeout=60,
                )

            # 1. 临时索引初始化为 HEAD 的树（同时验证仓库有 HEAD）
            if _git(["read-tree", "HEAD"]).returncode != 0:
                return None
            # 2. 把工作区全部改动（含未跟踪、删除）加入临时索引（只读工作树，写 object db）
            if _git(["add", "-A"]).returncode != 0:
                return None
            # 3. 临时索引写成 tree 对象
            wt = _git(["write-tree"])
            if wt.returncode != 0 or not wt.stdout.strip():
                return None
            tree_sha = wt.stdout.strip()
            # 4. 用该 tree + HEAD 为父，产出游离 commit（不动任何分支/HEAD/主索引）
            ct = _git([
                "commit-tree", tree_sha, "-p", "HEAD",
                "-m", "agent-parliament worktree snapshot (uncommitted changes)",
            ])
            if ct.returncode != 0 or not ct.stdout.strip():
                return None
            return ct.stdout.strip()
        except Exception:
            return None
        finally:
            # 清理临时索引文件，无论成败
            if tmp_index:
                try:
                    if os.path.exists(tmp_index):
                        os.unlink(tmp_index)
                except Exception:
                    pass

    def get_diff(self) -> str:
        """
        提取子 Agent 在 worktree 内的所有改动，返回 unified diff 文本。

        包含已暂存（staged）和未暂存（unstaged）的改动，以及未跟踪的新文件。
        主 Agent 拿到这份 diff 后决定是否合并——改动不直接落盘主仓库。
        """
        if self._destroyed:
            return ""

        # 先 git add -A 把未跟踪文件纳入跟踪，确保 diff 能捕获新建的测试文件
        # （git diff 默认不显示未跟踪文件，需要先 add 才能看到）
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self.path,
            capture_output=True, text=True, timeout=30,
        )
        # git diff --cached 提取所有已暂存改动（含刚才 add 的未跟踪文件）
        result = subprocess.run(
            ["git", "diff", "--cached"],
            cwd=self.path,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30,
        )
        return result.stdout if result.returncode == 0 else ""

    def destroy(self) -> None:
        """
        销毁 worktree 并删除临时分支。

        幂等：已销毁则直接返回。
        销毁失败（worktree 被占用、分支被引用等）记录警告但不抛异常——
        避免清理失败阻断主流程，孤儿 worktree 由 atexit 兜底再清一次。
        """
        if self._destroyed:
            return
        self._destroyed = True

        # 从全局表移除（无论销毁是否成功都先移除，避免 atexit 重复清理）
        with _WT_LOCK:
            _ACTIVE_WORKTREES.pop(self.path, None)

        # 销毁 worktree：--force 忽略未提交改动
        wt_result = subprocess.run(
            ["git", "worktree", "remove", self.path, "--force"],
            cwd=self.base_repo_dir,
            capture_output=True, text=True, timeout=60,
        )
        if wt_result.returncode != 0:
            # worktree 销毁失败：尝试 prune 清理元数据，记录但不中断
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self.base_repo_dir,
                capture_output=True, timeout=30,
            )

        # 删除临时分支：-D 强制删除（忽略未合并警告）
        subprocess.run(
            ["git", "branch", "-D", self.branch_name],
            cwd=self.base_repo_dir,
            capture_output=True, text=True, timeout=30,
        )

    def __enter__(self):
        """ 支持 with 语法：with WorktreeSession(dir) as wt: ... """
        self.create()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """ with 块结束时自动销毁，即使内部抛异常也清理 """
        self.destroy()
        return False  # 不吞异常


def _cleanup_all_worktrees() -> None:
    """
    MCP 服务退出时清理所有残留 worktree，避免孤儿（best-effort，强杀场景不触发 atexit）。
    参考 runner._cleanup_running_procs 的模式：先在锁内复制并清空列表，释放锁后逐个销毁。
    """
    with _WT_LOCK:
        sessions = list(_ACTIVE_WORKTREES.values())
        _ACTIVE_WORKTREES.clear()
    for session in sessions:
        try:
            session.destroy()
        except Exception:
            pass  # 清理失败不阻断退出


atexit.register(_cleanup_all_worktrees)


def list_active_worktrees() -> list[str]:
    """ 返回当前活跃 worktree 的路径列表（供诊断/调试用） """
    with _WT_LOCK:
        return list(_ACTIVE_WORKTREES.keys())
