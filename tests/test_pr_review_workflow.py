"""_reusable-pr-review.yml 裡兩段把關 shell 的行為測試。

為什麼要測：這兩段決定了「敏感內容會不會被送去 LLM」與「PR 會不會被放行」，
出錯的代價分別是個資外流與閘門失效，但它們內嵌在 YAML 的 run: block 裡，
沒有測試就沒人會發現改壞了。

為什麼是「從 YAML 抽出來跑」而不是把腳本複製一份到 tests/：
reusable workflow 的 actions/checkout 取的是**呼叫端** repo，本 repo 的
scripts/ 不會出現在執行環境裡，所以邏輯必須內嵌在 YAML。既然不能抽成檔案，
測試就直接讀 YAML 執行，這樣邏輯與測試永遠不可能漂移。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml  # 宣告在 requirements.txt；缺了要讓 CI 紅，不要靜默 skip

WORKFLOW = (
    Path(__file__).resolve().parents[1]
    / ".github"
    / "workflows"
    / "_reusable-pr-review.yml"
)

GUARD_STEP = "敏感路徑檢查"
PRECHECK_STEP = "決定是否執行 AI review"
VERDICT_STEP = "貼出 review 並套用閘門判定"

GUARD_104 = "\n".join([r"^fixtures/.*\.json$", "^reports/", r"\.db$", r"\.sqlite3?$"])
GUARD_ARENA = "\n".join(["^workspaces/", "^reports/", r"^data/arena\.db"])


def _step_script(step_name: str) -> str:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["review"]["steps"]:
        if step.get("name") == step_name:
            return step["run"]
    raise AssertionError(f"workflow 裡找不到名為 {step_name!r} 的 step")


def _fake_gh(tmp_path: Path, changed_files: str = "") -> Path:
    """假的 gh：回傳指定的檔案清單，其餘子指令記錄呼叫後成功返回。

    記錄檔讓測試能斷言「有沒有加 label」這種副作用。
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            echo "$@" >> "{tmp_path.as_posix()}/gh-calls.log"
            if [ "$1" = "pr" ] && [ "$2" = "diff" ]; then
              cat <<'CHANGED'
{changed_files}
CHANGED
              exit 0
            fi
            if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
              [ -f "{tmp_path.as_posix()}/gh-view-fail" ] && {{ echo "HTTP 502" >&2; exit 1; }}
              # gh-comments.txt：每行「作者login<TAB>留言內容」。模擬 --jq 的作者篩選：
              # 只有在 jq 式子真的寫了 select(.author.login=="github-actions" …) 時才只回 bot 的留言，
              # 否則回全部——workflow 若拿掉篩選，「偽造標記」的測試就會失敗。
              if [ -f "{tmp_path.as_posix()}/gh-comments.txt" ]; then
                case "$*" in
                  *'select(.author.login=="github-actions"'*)
                    while IFS=$'\\t' read -r login body; do
                      case "$login" in github-actions|'github-actions[bot]') printf '%s\\n' "$body";; esac
                    done < "{tmp_path.as_posix()}/gh-comments.txt";;
                  *) cut -f2- "{tmp_path.as_posix()}/gh-comments.txt";;
                esac
              fi
              exit 0
            fi
            exit 0
            """
        ),
        encoding="utf-8",
    )
    (bindir / "gh").chmod(0o755)
    # 有些 step 會呼叫 python3；Windows 的 Git Bash 不一定找得到，指向跑測試的這個直譯器
    (bindir / "python3").write_text(
        f'#!/usr/bin/env bash\nexec "{_msys(Path(sys.executable))}" "$@"\n',
        encoding="utf-8", newline="\n",
    )
    (bindir / "python3").chmod(0o755)
    return bindir


def _msys(p: Path) -> str:
    """C:\\Users\\x → /c/Users/x（Git Bash 的 PATH 認得的形式）。"""
    s = p.as_posix()
    if os.name == "nt" and len(s) > 1 and s[1] == ":":
        return f"/{s[0].lower()}{s[2:]}"
    return s


def _bash() -> str:
    """在 Windows 上明確挑 Git Bash。

    裸 `bash` 在裝了 WSL 的 Windows 會解析到 WSL 的 bash，它看到的是 /mnt/... 檔案系統，
    測試用的假 gh 不在它的 PATH 上，於是真的 gh 被呼叫、噴 git 錯誤——症狀完全不像
    「PATH 挑錯直譯器」，很容易往邏輯錯誤的方向查。
    """
    if os.name == "nt":
        for candidate in (
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(candidate).exists():
                return candidate
        pytest.skip("Windows 上找不到 Git Bash；這些測試在 CI（ubuntu）會跑")
    found = shutil.which("bash")
    if not found:
        pytest.skip("環境裡沒有 bash")
    return found


def _run(script: str, cwd: Path, bindir: Path, env: dict[str, str]):
    # 寫成檔案再執行，不要用 bash -c "<多行腳本>"：在 Windows 上 argv 會走 MSVC
    # 的引號規則，單引號與反斜線會被吃掉（printf '%s\n' 變成 printf %sn），
    # 腳本以完全不同的語意執行。
    script_file = cwd / "_step.sh"
    script_file.write_text(script, encoding="utf-8", newline="\n")

    # PATH 在 bash 裡是用 `:` 分隔的，而 Windows 的 PATH 每一項都含磁碟機冒號
    # （C:\Windows;...）。把兩者串起來會被切成一堆碎片，假 gh 進不了搜尋路徑，
    # 真的 gh 被呼叫，錯誤訊息看起來完全像是腳本邏輯壞掉。
    # Windows 上改成只給 MSYS 形式的 bindir + Git Bash 自己的 /usr/bin。
    if os.name == "nt":
        path = f"{_msys(bindir)}:/usr/bin:/bin"
    else:
        path = f"{bindir}{os.pathsep}{os.environ['PATH']}"

    full_env = {
        **os.environ,
        "PR": "1",
        "PATH": path,
        **env,
    }
    return subprocess.run(
        [_bash(), str(script_file)],
        cwd=cwd,
        env=full_env,
        capture_output=True,
        # 不指定就會用系統 locale（Windows 上是 cp950），中文輸出直接 UnicodeDecodeError。
        encoding="utf-8",
        errors="replace",
    )


def _calls(tmp_path: Path) -> str:
    log = tmp_path / "gh-calls.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ---------------------------------------------------------------- guard


@pytest.mark.parametrize(
    "changed, guard, blocked, why",
    [
        ("parser.py\napp.py\nfixtures/README.md", GUARD_104, False, "一般改動放行"),
        ("parser.py\nfixtures/sample_detail.json", GUARD_104, True, "真實履歷 fixture"),
        ("talents.sqlite3", GUARD_104, True, "sqlite 資料庫"),
        ("notes.db", GUARD_104, True, "db 副檔名"),
        ("reports/a.md\nfixtures/x.json\nb.db", GUARD_104, True, "多條 pattern 同時命中"),
        ("src/arena/cli.py\nworkspaces/c01/main.py", GUARD_ARENA, True, "考生工作區"),
        ("src/arena/cli.py", GUARD_ARENA, False, "arena 一般改動放行"),
        # guard_paths 是 YAML block scalar，尾端與中間都可能夾空行
        ("parser.py", "^reports/\n\n^workspaces/\n", False, "guard_paths 含空行不誤判"),
    ],
)
def test_guard_blocks_only_sensitive_paths(tmp_path, changed, guard, blocked, why):
    bindir = _fake_gh(tmp_path, changed)
    res = _run(
        _step_script(GUARD_STEP),
        tmp_path,
        bindir,
        {"GUARD_PATHS": guard, "GUARD_REASON": "測試用原因"},
    )
    assert (res.returncode != 0) is blocked, f"{why}: {res.returncode=} {res.stderr=}"
    if blocked:
        # 擋下時必須貼上 needs-work，否則閘門判定那步會誤放行
        assert "pr edit 1 --add-label needs-work" in _calls(tmp_path), why
        body = (tmp_path / "guard-comment.md").read_text(encoding="utf-8")
        assert "不該進版控" in body and "git rm --cached" in body, why


def test_guard_comment_lists_every_hit(tmp_path):
    bindir = _fake_gh(tmp_path, "reports/a.md\nfixtures/x.json\nkeep.py")
    _run(
        _step_script(GUARD_STEP),
        tmp_path,
        bindir,
        {"GUARD_PATHS": GUARD_104, "GUARD_REASON": "r"},
    )
    body = (tmp_path / "guard-comment.md").read_text(encoding="utf-8")
    assert "reports/a.md" in body and "fixtures/x.json" in body
    assert "keep.py" not in body, "沒命中的檔案不該出現在擋下留言裡"


# ------------------------------------------------------------- precheck


def _precheck(tmp_path: Path, author: str, author_type: str, token: str) -> dict[str, str]:
    """跑 precheck step，回傳它寫進 $GITHUB_OUTPUT 的 key/value。"""
    out = tmp_path / "gh-output.txt"
    out.write_text("", encoding="utf-8")
    res = _run(
        _step_script(PRECHECK_STEP),
        tmp_path,
        _fake_gh(tmp_path),
        {
            "GITHUB_OUTPUT": out.as_posix(),
            "PR_AUTHOR": author,
            "PR_AUTHOR_TYPE": author_type,
            "TOKEN": token,
        },
    )
    assert res.returncode == 0, f"precheck 不該非零離開: {res.stderr}"
    parsed = dict(
        line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if line
    )
    return {**parsed, "_stdout": res.stdout}


@pytest.mark.parametrize(
    "author, author_type, skip, why",
    [
        ("gsinvest017-ai", "User", False, "人類的 PR 照常 review"),
        ("github-actions[bot]", "Bot", False, "auto-fix loop 自己開的 PR 最需要被審"),
        ("dependabot[bot]", "Bot", True, "dependabot：allowed_bots 沒放行，會紅燈"),
        ("renovate[bot]", "Bot", True, "白名單而非黑名單——沒列名的 bot 預設跳過"),
    ],
)
def test_precheck_only_reviews_humans_and_github_actions(
    tmp_path, author, author_type, skip, why
):
    """機器人的 PR 必須在呼叫 action 之前就跳過。

    沒擋掉的話 claude-code-action 會以「Workflow initiated by non-human actor」
    exit 1，變成一個 PR 作者無從處理的紅燈——實例見 PR #18 / run 30699145258。
    """
    assert _precheck(tmp_path, author, author_type, "tok")["skip"] == str(skip).lower(), why


def test_precheck_skips_cleanly_without_token(tmp_path):
    """缺 secret 要乾淨跳過，不是讓整個 job 紅掉。"""
    result = _precheck(tmp_path, "gsinvest017-ai", "User", "")
    assert result["skip"] == "true"
    assert "CLAUDE_CODE_OAUTH_TOKEN" in result["_stdout"], "要留下告訴 repo 主人怎麼補的訊息"


def test_precheck_allowlist_matches_action_allowed_bots():
    """precheck 放行的 bot 必須也在 action 的 allowed_bots 裡，否則照樣紅燈。

    這兩份名單分屬不同檔案區塊，很容易只改一邊。
    """
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    script = _step_script(PRECHECK_STEP)
    for step in doc["jobs"]["review"]["steps"]:
        if step.get("uses", "").startswith("anthropics/claude-code-action"):
            allowed = step["with"]["allowed_bots"]
            assert "github-actions" in allowed
            assert "github-actions[bot]" in script, "precheck 放行的 bot 與 allowed_bots 不一致"
            return
    raise AssertionError("找不到 claude-code-action step")


# -------------------------------------------------------------- verdict


@pytest.mark.parametrize(
    "verdict_line, expect_label",
    [
        ("NEEDS_WORK: no", False),
        ("NEEDS_WORK: yes", True),
        ("NEEDS_WORK:   yes  ", True),
        ("", True),  # 沒有判定行 → fail closed
        ("NEEDS_WORK: maybe", True),  # 無法辨識 → fail closed
    ],
)
def test_verdict_fails_closed(tmp_path, verdict_line, expect_label):
    bindir = _fake_gh(tmp_path)
    (tmp_path / "review-output.md").write_text(
        f"## 這個 PR 做了什麼\n改了一行\n{verdict_line}\n", encoding="utf-8"
    )
    res = _run(_step_script(VERDICT_STEP), tmp_path, bindir, {})
    assert res.returncode == 0, res.stderr
    added = "pr edit 1 --add-label needs-work" in _calls(tmp_path)
    assert added is expect_label, f"{verdict_line!r} -> {added=}"


def test_verdict_line_stripped_from_comment(tmp_path):
    bindir = _fake_gh(tmp_path)
    (tmp_path / "review-output.md").write_text(
        "## 結論\n可以合 ✅\nNEEDS_WORK: no\n", encoding="utf-8"
    )
    _run(_step_script(VERDICT_STEP), tmp_path, bindir, {})
    comment = (tmp_path / "comment.md").read_text(encoding="utf-8")
    assert "NEEDS_WORK" not in comment, "判定標記不該外洩到給作者看的留言裡"
    assert "可以合" in comment


def test_missing_review_output_is_blocked(tmp_path):
    """模型沒產出檔案（逾時、被注入干擾）時必須擋下，不能默默放行。"""
    bindir = _fake_gh(tmp_path)
    res = _run(_step_script(VERDICT_STEP), tmp_path, bindir, {})
    assert res.returncode == 0
    assert "pr edit 1 --add-label needs-work" in _calls(tmp_path)


# ------------------------------------------------------- 留言前祕密檢查

LEAK_STEP = "留言前祕密檢查"
FAKE_SECRET = "fake-secret-VALUE-0123456789abcdef"
EMPTY_LEAK_ENV = {"LEAK_GITHUB_TOKEN": "", "LEAK_CLAUDE_TOKEN": "", "LEAK_GDECK_KEY": ""}


def _leak_check(tmp_path: Path, text: str | None, env: dict[str, str]):
    if text is not None:
        (tmp_path / "review-output.md").write_text(text, encoding="utf-8")
    return _run(_step_script(LEAK_STEP), tmp_path, _fake_gh(tmp_path), {**EMPTY_LEAK_ENV, **env})


def test_leak_check_passes_clean_review(tmp_path):
    res = _leak_check(tmp_path, "## 結論\n可以合，順帶一提 `ghp_` 是 PAT 的前綴\nNEEDS_WORK: no\n",
                      {"LEAK_GITHUB_TOKEN": "ghs_realvalue000000000000000000000000"})
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "review-output.md").exists(), "乾淨的 review 要留給下一步貼"
    assert "pr comment" not in _calls(tmp_path)


@pytest.mark.parametrize("env_name", ["LEAK_GITHUB_TOKEN", "LEAK_CLAUDE_TOKEN", "LEAK_GDECK_KEY"])
def test_leak_check_blocks_literal_secret(tmp_path, env_name):
    res = _leak_check(tmp_path, f"## 結論\n這是你要的：{FAKE_SECRET}\nNEEDS_WORK: no\n",
                      {env_name: FAKE_SECRET})
    assert res.returncode != 0
    assert FAKE_SECRET not in res.stdout + res.stderr, "secret 值絕不能出現在 log"
    assert env_name in res.stdout + res.stderr, "要說出命中哪一項（名稱，不是值）"
    calls = _calls(tmp_path)
    assert "pr comment 1 --body-file leak-blocked.md" in calls
    assert "pr edit 1 --add-label needs-work" in calls
    assert FAKE_SECRET not in (tmp_path / "leak-blocked.md").read_text(encoding="utf-8")
    assert not (tmp_path / "review-output.md").exists(), "模型輸出要刪掉，不能被之後的步驟貼出"


@pytest.mark.parametrize(
    "token",
    ["ghp_" + "a1B2" * 9, "ghs_" + "Z" * 36, "github_pat_" + "x1_" * 20, "sk-ant-oat01-" + "Q" * 40],
)
def test_leak_check_blocks_token_patterns(tmp_path, token):
    res = _leak_check(tmp_path, f"結論 {token}\nNEEDS_WORK: no\n", {})
    assert res.returncode != 0
    assert token not in res.stdout + res.stderr
    assert "pr comment 1 --body-file leak-blocked.md" in _calls(tmp_path)


def test_leak_check_empty_secrets_do_not_false_positive(tmp_path):
    """secret 沒設（空字串）時 grep -F '' 會永遠成立——必須跳過，不能把每則 review 都擋掉。"""
    res = _leak_check(tmp_path, "## 結論\n沒問題\nNEEDS_WORK: no\n", {})
    assert res.returncode == 0, res.stderr
    assert (tmp_path / "review-output.md").exists()


def test_leak_check_missing_output_defers_to_fail_closed(tmp_path):
    res = _leak_check(tmp_path, None, {"LEAK_GITHUB_TOKEN": FAKE_SECRET})
    assert res.returncode == 0, res.stderr
    assert _calls(tmp_path) == ""


def test_leak_check_runs_before_posting():
    """順序不變式：祕密檢查必須在唯一的貼 review 步驟之前，且條件相同。"""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    names = [s.get("name") for s in doc["jobs"]["review"]["steps"]]
    assert names.index(LEAK_STEP) < names.index(VERDICT_STEP)
    steps = {s.get("name"): s for s in doc["jobs"]["review"]["steps"]}
    assert steps[LEAK_STEP]["if"] == steps[VERDICT_STEP]["if"]
    # 只有這兩個 step 會把 review-output.md 相關內容貼成留言
    posting = [s.get("name") for s in doc["jobs"]["review"]["steps"]
               if "review-output.md" in (s.get("run") or "") and "gh pr comment" in (s.get("run") or "")]
    assert set(posting) <= {LEAK_STEP, VERDICT_STEP, FAILCLOSED_STEP}, posting


def test_leak_check_blocks_jwt_shape(tmp_path):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
    res = _leak_check(tmp_path, f"結論 {jwt}\nNEEDS_WORK: no\n", {})
    assert res.returncode != 0
    assert jwt not in res.stdout + res.stderr


# ------------------------------------------------------- 外部圖片中和
#
# fix round 4 的規則（刻意簡單）：
#   - 唯一的豁免是「乾淨的頂層 fenced code block」：整份文件只有散文＋行首、不縮排、有關好、
#     info string 單純且不是 GitHub 會渲染的語言（mermaid 等）的 fence 時，fence 內一字不改；
#   - 文件裡只要出現引言、清單、註腳定義、行首的 <、任何縮排行、或任何不合格的 fence，
#     整份文件就沒有豁免；
#   - 沒被豁免的地方（含行內 code）每個 `![` 都加反斜線、圖片／嵌入類標籤的 `<` 都改成 &lt;。


def _posted_comment(tmp_path: Path, body: str) -> str:
    (tmp_path / "review-output.md").write_text(body + "\nNEEDS_WORK: no\n", encoding="utf-8")
    res = _run(_step_script(VERDICT_STEP), tmp_path, _fake_gh(tmp_path), {})
    assert res.returncode == 0, res.stderr
    assert "pr comment 1 --body-file comment.md" in _calls(tmp_path)
    return (tmp_path / "comment.md").read_text(encoding="utf-8")


import re as _re

_UNESCAPED_IMG = _re.compile(r"(?<!\\)!\[")
_HTML_IMG = _re.compile(
    r"<(img|image|picture|source|video|audio|track|embed|object|iframe|svg|math|input|link|meta|base)", _re.I)


def _assert_no_image(out: str):
    """沒有任何東西能被 GitHub 渲染成圖片：沒有未跳脫的 `![`、沒有圖片類 HTML 標籤。"""
    assert not _UNESCAPED_IMG.search(out), out
    assert not _HTML_IMG.search(out), out


def _same(body: str, out: str):
    assert out in (body, body + "\n"), f"程式碼必須一字不改:\n{body!r}\n→\n{out!r}"


def test_remote_images_are_neutralized(tmp_path):
    body = (
        "看這張 ![x](https://evil.example/?d=AAAA) 圖\n"
        '<img src="https://evil.example/x">\n'
        '<IMG\n  src="https://evil.example/multiline">\n'
        "![ref][r]\n\n[r]: https://evil.example/ref\n"
        "<picture><source srcset=\"https://evil.example/s\"></picture>\n"
        "一般連結 [link](https://example.com) 要保留\n"
    )
    out = _posted_comment(tmp_path, body)
    _assert_no_image(out)
    assert not _rendered_fetching_tags(out), out
    # 只跳脫、不刪字元：網址會以文字留在留言裡，但不會被自動抓取
    assert "!\\[x](https://evil.example/?d=AAAA)" in out, out
    assert '&lt;img src="https://evil.example/x">' in out, out
    assert "[link](https://example.com)" in out, "一般連結不會被自動抓取，必須保留"


def test_clean_comment_unchanged(tmp_path):
    body = "## 這個 PR 做了什麼\n改了 `a < b` 的判斷，見 [文件](https://example.com/doc)。\n\n- 項目一\n"
    _same(body, _posted_comment(tmp_path, body))


IDSP, NBSP, LSEP, PSEP, BOM = chr(0x3000), chr(0xA0), chr(0x2028), chr(0x2029), chr(0xFEFF)  # 以碼位寫，避免原始碼裡出現這些字元
# fix round 3：Python 的 splitlines()/strip() 比 cmark 認更多換行與空白，兩邊對「什麼是程式碼」會錯開
NONSTD_WS_CASES = [
    *[(f"`a\n{ws}\n`![x](https://evil.example/a)`\n", f"{ws!r} 行不是空行，段落沒斷")
      for ws in (IDSP, NBSP, "\f", "\v", "\x85", LSEP, PSEP, "\x1c")],
    *[(f"<div>\n{ws}\n`<img src=https://evil.example/a>`\n</div>\n", f"{ws!r} 不會結束 HTML block")
      for ws in (IDSP, NBSP)],
    *[(f"X {sep}~~~\n![x](https://evil.example/a)\nY ~~~\n", f"{sep!r} 不是換行，~~~ 不在行首")
      for sep in ("", "\f", "\v", "\x85", LSEP, PSEP, "\x1c", "\x1d", "\x1e")],
]


# --- 豁免：散文＋乾淨的頂層 fence，fence 內一字不改

RUST_FENCE = "```rust\nlet v = vec![1, 2];\nlet f = vec![x][0];\nassert!(matches!(x, [..]));\n```\n"
JSX_FENCE = '```tsx\nconst A = () => <img src={logo} />;\n```\n'
PLAIN_REVIEW = (
    "## 這個 PR 做了什麼\n改了解析流程，見 [文件](https://example.com/doc)。\n\n"
    + RUST_FENCE
    + "\n**問題是什麼** → 少了 alt；`a < b` 的判斷沒問題。\n\n"
    + JSX_FENCE
    + "\n## 結論\n可以合 ✅\n"
)


@pytest.mark.parametrize(
    "body",
    [
        PLAIN_REVIEW,
        RUST_FENCE,
        JSX_FENCE,
        "~~~\nlet v = vec![1, 2];\n~~~\n",
        "````md\n```\nvec![x][0]\n```\n````\n",          # 外層較長的 fence 包住內層
        "前文\n\n" + RUST_FENCE + "\n後文 [link](https://example.com)\n",
        # fence「裡面」的縮排、清單符號、>、< 都是程式碼，不算疑慮
        "```yaml\n- a: vec![1]\n  b:\n    - <img src=x>\n> q\n\tt\n```\n",
        "```\nvec![1]\n```",                               # 結尾沒有換行
    ],
)
def test_clean_top_level_fences_are_left_byte_for_byte(tmp_path, body):
    _same(body, _posted_comment(tmp_path, body))
    assert _neutralizer_module()["neutralize"](body) == body


def test_inline_code_is_escaped_accepted_cost():
    """行內 code 不豁免：`vec![1]` 會多一個反斜線、`<img …>` 會變成 &lt;img（已接受的代價）。"""
    neutralize = _neutralizer_module()["neutralize"]
    assert neutralize("用 `vec![1]` 即可\n") == "用 `vec!\\[1]` 即可\n"
    assert neutralize("JSX 寫成 `<img src={logo} />`\n") == "JSX 寫成 `&lt;img src={logo} />`\n"


@pytest.mark.parametrize(
    "extra, why",
    [
        ("- 項目一\n", "清單"),
        ("* 項目一\n", "清單（*）"),
        ("1. 第一點\n", "有序清單"),
        ("2) 第二點\n", "有序清單（括號）"),
        ("> 引言\n", "引言"),
        ("<details>\n", "行首的 <（可能是 HTML block）"),
        ("x[^1]\n\n[^1]: 註腳\n", "註腳定義"),
        ("    四格縮排\n", "縮排行"),
        (" 一格縮排\n", "縮排行（1 格也算）"),
        ("\ttab 縮排\n", "tab 縮排"),
        ("~~~~\n沒關的 fence（後面的 ``` 與較短的 ~~~ 都關不掉它）\n", "沒關的 fence"),
        (" ```\nx\n ```\n", "縮排的 fence"),
        ("```\nx\n  ```\n", "收尾 fence 有縮排"),
        ("```js title\nx\n```\n", "info string 不單純"),
        ("```mermaid\ngraph TD\n```\n", "GitHub 會渲染的語言"),
        ("```GeoJSON\n{}\n```\n", "GitHub 會渲染的語言（不分大小寫）"),
        ("~~~math\nx\n~~~\n", "GitHub 會渲染的語言（~~~）"),
    ],
)
def test_any_doubt_removes_exemption_for_whole_document(extra, why):
    """文件裡只要有一處讓頂層結構不確定，整份文件的 fence 都不豁免（文件化的行為）。"""
    neutralize = _neutralizer_module()["neutralize"]
    for body in (PLAIN_REVIEW + "\n" + extra, extra + "\n" + PLAIN_REVIEW):
        out = neutralize(body)
        assert "let v = vec!\\[1, 2];" in out, f"{why}: {out!r}"
        assert "&lt;img src={logo} />" in out, f"{why}: {out!r}"
        assert "matches!(x, [..])" in out, "沒有 `![` 的程式碼不會被動到"
        _assert_no_image(out)
        assert not _rendered_fetching_tags(out), f"{why}: {out!r}"


def test_mermaid_fence_is_not_exempt_and_not_rendered():
    """mermaid 支援圖片節點；GitHub 會渲染 mermaid／geojson／topojson／stl／math 的 fence。"""
    body = (
        "流程如下\n\n```mermaid\nflowchart TD\n"
        '  A@{ img: "https://evil.example/m.png", label: "x" }\n'
        "  B[\"<img src='https://evil.example/n.png'>\"]\n```\n\n" + RUST_FENCE
    )
    out = _neutralizer_module()["neutralize"](body)
    assert "```mermaid" not in out and "```text mermaid" in out, out
    assert "<img" not in out
    assert "vec!\\[1, 2]" in out, "有 mermaid fence 的文件整份不豁免"
    assert not _rendered_fetching_tags(out), out
    # 量測工具本身要看得到「會被渲染的語言」
    assert _rendered_fetching_tags("```mermaid\ngraph TD\n```\n") == ["lang"] * len(_RENDER_OPTS)


@pytest.mark.parametrize(
    "body",
    [
        "let v = vec![1, 2];\n",
        "let f = vec![x][0];\n",
        "![x](https://evil.example/?d=A)\n",
        "![a][r]\n\n[r]: https://evil.example/r\n",
        "![r]\n\n[r]: https://evil.example/r\n",
        "![a][]\n\n[a]: https://evil.example/r\n",
        '<IMG\nsrc="https://evil.example/x">\n',
        "<image src=https://evil.example/i>\n",
        "<video poster=https://evil.example/p></video>\n",
        "<img/src=https://evil.example/x>\n",
        "!![x](y)(https://evil.example/recombine)\n",                 # 換掉後不能拼出新圖片
        "<im<img x>g src=https://evil.example/r>\n",
    ],
)
def test_images_outside_code_never_survive(tmp_path, body):
    out = _posted_comment(tmp_path, body)
    _assert_no_image(out)
    assert not _rendered_fetching_tags(out), out


@pytest.mark.parametrize(
    "body, why",
    [
        ("```\n![x](https://evil.example/a)\n", "沒關的 fence：一律當成非程式碼，剝掉"),
        ("````\n![x](https://evil.example/a)\n```\n", "關閉 fence 比開頭短 → 沒關"),
        ("`foo ![x](https://evil.example/a)\n", "沒關的行內反引號 → 不是程式碼"),
        ("`foo\n![x](https://evil.example/a) `\n", "行內程式碼不跨行"),
        ("    ![x](https://evil.example/a)\n", "4 格縮排不當程式碼"),
        ("<!--\n```\n-->\n![x](https://evil.example/a)\n```\n", "HTML 註解裡的 fence 不算"),
        ("  ```\nfoo\n```\n![x](https://evil.example/a)\n```\n", "縮排 fence 讓狀態不確定 → 全部不當程式碼"),
        ("- item\n\n  ```\nx\n  ```\nfoo\n  ```\n  ![x](https://evil.example/a)\n  ```\n", "清單內 fence"),
        ("> ```\n![x](https://evil.example/a)\n> ```\n", "blockquote 內 fence"),
        ("<div>\n`<img src=https://evil.example/a>`\n</div>\n", "HTML block 裡反引號不是程式碼"),
        ("<pre>\n\n`<img src=https://evil.example/a>`\n</pre>\n", "HTML block type 1 跨空行"),
        ('[a](x "`") ![b](https://evil.example/a) ("`")\n', "連結 title 裡的反引號"),
        ('<a title="`">![b](https://evil.example/a)<a title="`">\n', "HTML 屬性裡的反引號"),
        ('<a title="\n`"> ![b](https://evil.example/a) `\n', "跨行 HTML 屬性"),
        ("| `a | ![x](https://evil.example/a) | b` |\n|---|---|---|\n", "表格的 | 會切開行內程式碼"),
        ("\\``x` ![x](https://evil.example/a)`\n", "跳脫的反引號"),
        ("```x`y\n![x](https://evil.example/a)\n```\n", "info string 含反引號的不是 fence"),
        ("`a\nb` ![x](https://evil.example/a) `c`\n", "跨行配對會讓單行判斷錯開"),
        ("`a|b` ![x](https://evil.example/a) `c|d`\n", "不是表格時 | 在 code 裡"),
        ("$a`$ ![x](https://evil.example/a) $`b$\n", "數學 $ 可能先吃掉反引號"),
        ("| `a | ![x](https://evil.example/a) | b` |\r\n|---|---|---|\r\n", "CRLF 的表格"),
        ("<details>\n```\n-->\n![x](https://evil.example/a)\n```\n", "HTML block 裡的 fence"),
        # fix round 3：fuzz（對 cmark-gfm 差分）找到的
        ("https://e/d```x<img src=https://evil.example/a>w```\n", "GFM 延伸 autolink 吃掉反引號"),
        ("www.e.com```x<img src=https://evil.example/a>w```\n", "www. autolink"),
        ("``````x `y ```x` y ```<img src=https://evil.example/a>```\n",
         "cmark 的反引號快取：前面有配不到的串，後面本來配得到的也不配"),
        ("<span>x\n<!--\n\n```x<img/src=https://evil.example/a>```\n", "HTML 模式中途出現的 <!-- 要等 -->"),
        ("<pre>\n<!--\n-->\n\n`<img src=https://evil.example/a>`\n</pre>\n", "巢狀的結束記號都要看到"),
        ("<img/src=x>\n<!--a\n<img x]-->```<img/src=x>\n\nword ```<video poster=https://evil.example/a>```\n",
         "替換範圍吃到 --> 會讓後面變成 raw HTML"),
        ("![a](u\n\n)```<img src=https://evil.example/a>```\n", "跨空行的替換會併掉段落"),
        # fix round 4：第三輪 review 的獨立 fuzzer 找到的（round 3 的實作原樣輸出、渲染出圖片）
        ("> | `a | ![x](https://ev.il/s) | b` |\n> |---|---|---|\n", "引言裡的表格"),
        ("x[^1]\n\n[^1]: <pre>\n\n    `<img src=https://ev.il/s>`\n", "註腳定義裡的 HTML block"),
        ("x[^1]\n\n[^1]:<script>\n\n    ``<IMG SRC='https://ev.il/s'/>``\n", "註腳定義裡的 <script>"),
        ("x[^1]\n\n[^1]:<!X\n    | --- | :-: |\n\n    | `a | ![*a*](https://ev.il/s 't') | b` |\n", "註腳＋宣告＋表格"),
        ("- | `a | <video poster=https://ev.il/s></video> | b` |\n  |-|-|-|\n", "清單裡的表格"),
        ("```mermaid\ngraph TD\n  A[\"<img src='https://ev.il/s'>\"]\n```\n", "mermaid fence"),
        ("<embed src=https://ev.il/s>\n<object data=https://ev.il/s></object>\n"
         "<iframe src=https://ev.il/s></iframe>\n<input type=image src=https://ev.il/s>\n", "嵌入類標籤"),
        ("```\nx\n   ```\n![x](https://ev.il/s)\n```\ny\n```\n", "縮排的收尾 fence 讓兩邊開關錯開"),
        ("```\nx\n````\n![x](https://ev.il/s)\n```\n", "較長的收尾"),
        ("~~~\nx\n```\n~~~\n![x](https://ev.il/s)\n", "不同字元不收尾"),
        ("foo\n```rust\n![x](https://ev.il/s)\n", "fence 打斷段落但沒關"),
        ("   ```\n```\n<img src=https://ev.il/s>\n```\n```\n", "縮排的開頭 fence 讓後面的開關整個錯開"),
        ("<div>\n```\n<img src=https://ev.il/s>\n```\n", "HTML block 裡的行首 fence 不是程式碼"),
        # cmark 只會去掉一個開頭的 BOM：只去一個的話，第一行在這裡看不是 <div>、在 GitHub 看是
        (BOM + "<div>\n```\n<img src=https://ev.il/s>\n```\n", "BOM 後面的 HTML block"),
        (BOM + BOM + "<div>\n```\n<img src=https://ev.il/s>\n```\n", "兩個 BOM 後面的 HTML block"),
        ("x\r```\r<img src=https://ev.il/s>\r```\r", "只有 CR 的行尾"),
        ("```&#109;ermaid\ngraph TD\n```\n", "info string 裡的 entity 會被 cmark 解開成 mermaid"),
    ]
    + NONSTD_WS_CASES,
)
def test_stripper_cannot_be_tricked_into_treating_images_as_code(tmp_path, body, why):
    out = _posted_comment(tmp_path, body)
    # 以 GitHub 的 parser（cmark-gfm）渲染結果為準：真正的 code span 裡留著 `![` 是正確的
    assert not _rendered_fetching_tags(out), f"{why}: {out!r}"


def test_oversized_output_fails_closed(tmp_path):
    (tmp_path / "review-output.md").write_text("a" * 300_000 + "\nNEEDS_WORK: no\n", encoding="utf-8")
    res = _run(_step_script(VERDICT_STEP), tmp_path, _fake_gh(tmp_path), {})
    assert res.returncode != 0
    calls = _calls(tmp_path)
    assert "--body-file comment.md" not in calls, "超過上限不能貼模型的文字"
    assert "pr edit 1 --add-label needs-work" in calls
    assert "pr comment 1 --body-file review-failed.md" in calls
    assert FAILED_MARKER in (tmp_path / "review-failed.md").read_text(encoding="utf-8")
    assert not (tmp_path / "comment.md").exists() and not (tmp_path / "review-output.md").exists()


def test_neutralizer_crash_fails_closed(tmp_path):
    """python 那段出錯（這裡用一個會讓它崩潰的 python3 替身）時不能貼出模型文字。"""
    bindir = _fake_gh(tmp_path)
    (bindir / "python3").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8", newline="\n")
    (tmp_path / "review-output.md").write_text("ok\nNEEDS_WORK: no\n", encoding="utf-8")
    res = _run(_step_script(VERDICT_STEP), tmp_path, bindir, {})
    assert res.returncode != 0
    calls = _calls(tmp_path)
    assert "--body-file comment.md" not in calls
    assert "pr edit 1 --add-label needs-work" in calls


def _neutralizer_module() -> dict:
    """把 step 裡 heredoc 的 python 原文抽出來，當成模組載入（不執行 main）。"""
    script = _step_script(VERDICT_STEP)
    m = _re.search(r"<<'PY'\n(.*?)\nPY\n", script, _re.S)
    assert m, "找不到 python heredoc"
    ns: dict = {"__name__": "neutralize_under_test"}
    exec(compile(m.group(1), "neutralize", "exec"), ns)
    return ns


@pytest.mark.parametrize(
    "name, evil",
    [
        ("html 註解開頭", "<!--\n" * 40_000),                     # round 3 的 pending 清單：80 KB 要 11 秒
        ("很深的 label", "![ " * 44_000 + "]" * 44_000),          # round 2：約 31 秒
        ("反引號", "`" * 200_000),
        ("img 標籤", "<img " * 40_000),
        ("很多 fence", "```\n" * 50_000),
        ("沒關的 fence", "```\n" + "x\n" * 99_000),
        ("fence 後接很多空白", ("```" + " " * 5_000 + "!\n") * 39),
        ("數字開頭", "1" * 199_000 + "\n```\nx\n```\n"),
        ("混合", "`a` ![x](y) `` b `` <img z> ```mermaid " * 5_000),
    ],
)
def test_neutralizer_is_linear_on_pathological_input(name, evil):
    assert len(evil.encode("utf-8")) <= 200_000, "要在 200 KB 上限內才有意義"
    neutralize = _neutralizer_module()["neutralize"]
    t = _time.perf_counter()
    out = neutralize(evil)
    elapsed = _time.perf_counter() - t
    assert elapsed < 1.0, f"{name}: {elapsed:.2f}s"
    _assert_no_image(out)


# ------------------------------------------------------- 用 GitHub 的 parser 做差分測試
#
# cmarkgfm 是 github/cmark-gfm 的 Python binding，只在測試用（requirements.txt），
# workflow 執行時不需要。把 neutralize() 的「輸出」用 GitHub 風格設定渲染（GFM 擴充全開、
# 允許 raw HTML），再檢查渲染結果，下列任一種出現就算失敗：
#   1. html.parser 解析到會抓資源的元素（img／picture／source／video／audio／embed／object／iframe，
#      另外加 image、svg、type=image 的 input）；
#   2. 渲染後的 HTML 原文裡有這些標籤的開頭（html.parser 與瀏覽器對沒關的註解／宣告看法可能不同，
#      用原文比對補上，偏向多報）；
#   3. 有 code block 的語言是 GitHub 會渲染的（mermaid／geojson／topojson／stl／math）——
#      cmark 只會把它當程式碼，這一項是替「GitHub 之後會再渲染它」把關。
# 裝不起來時測試要失敗（import 錯誤），不能 skip。
import time as _time  # noqa: E402

import cmarkgfm  # noqa: E402
from cmarkgfm.cmark import Options as _CmarkOpts  # noqa: E402
from html.parser import HTMLParser  # noqa: E402

_FETCHING_TAGS = {"img", "image", "picture", "source", "video", "audio", "embed", "object", "iframe", "svg"}
_RAW_FETCHING = _re.compile(r"<(?:img|image|picture|source|video|audio|embed|object|iframe|svg)\b", _re.I)
_RENDERED_LANG = _re.compile(r'(?:class="language-|lang=")[^"]*(?:mermaid|geojson|topojson|stl|math)', _re.I)
_RENDER_OPTS = [
    _CmarkOpts.CMARK_OPT_UNSAFE,
    _CmarkOpts.CMARK_OPT_UNSAFE | _CmarkOpts.CMARK_OPT_LIBERAL_HTML_TAG | _CmarkOpts.CMARK_OPT_FOOTNOTES,
    _CmarkOpts.CMARK_OPT_UNSAFE | _CmarkOpts.CMARK_OPT_FOOTNOTES | _CmarkOpts.CMARK_OPT_GITHUB_PRE_LANG,
]


class _TagCollector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _FETCHING_TAGS:
            self.found.append(tag)
        elif tag == "input" and (dict(attrs).get("type") or "").lower() == "image":
            self.found.append("input[type=image]")  # 清單的核取方塊是 type=checkbox，不算

    handle_startendtag = handle_starttag


def _rendered_fetching_tags(md: str) -> list[str]:
    found: list[str] = []
    for opts in _RENDER_OPTS:
        html = cmarkgfm.github_flavored_markdown_to_html(md, options=opts)
        p = _TagCollector()
        p.feed(html)
        p.close()
        found += p.found
        found += ["raw:" + m.lower() for m in _RAW_FETCHING.findall(html) if not p.found]
        found += ["lang"] * bool(_RENDERED_LANG.search(html))
    return found


def test_differential_harness_detects_images():
    """先確認量測工具本身有效：未處理的圖片要被抓到，跳脫過的不算。"""
    assert _rendered_fetching_tags("![x](https://e/a)")
    assert _rendered_fetching_tags("<div>\n<img src=x>\n</div>\n")
    assert _rendered_fetching_tags("<image src=x>")
    assert _rendered_fetching_tags("<!X\n<source src=x>\n"), "宣告／註解裡的標籤用原文比對抓"
    # <iframe> 不在這裡：GFM 的 tagfilter 擴充本來就會把它轉成文字（_FETCHING_TAGS 仍列著，多一層）
    for tag in ("embed src=x", "object data=x", "input type=image src=x", "video poster=x"):
        assert _rendered_fetching_tags(f"<{tag}>"), tag
    assert _rendered_fetching_tags("~~~ Mermaid\ngraph TD\n~~~\n")
    assert not _rendered_fetching_tags("`<img src=x>` 與 !\\[x](y) 與 &lt;img src=x>")
    assert not _rendered_fetching_tags("- [ ] 核取方塊不是圖片\n\n```text mermaid\nx\n```\n")
    assert not _rendered_fetching_tags(RUST_FENCE + JSX_FENCE)


def _all_bypass_cases() -> list[str]:
    params = []
    for mark in test_stripper_cannot_be_tricked_into_treating_images_as_code.pytestmark:
        if mark.name == "parametrize":
            params += [b for b, _ in mark.args[1]]
    for mark in test_images_outside_code_never_survive.pytestmark:
        if mark.name == "parametrize":
            params += list(mark.args[1])
    params.append(
        "看這張 ![x](https://evil.example/?d=AAAA) 圖\n<img src=\"https://evil.example/x\">\n"
        "![ref][r]\n\n[r]: https://evil.example/ref\n"
    )
    return params


def test_differential_all_bypass_cases_render_no_images():
    ns = _neutralizer_module()
    bad = []
    for doc in _all_bypass_cases():
        tags = _rendered_fetching_tags(ns["neutralize"](doc))
        if tags:
            bad.append((doc, tags))
    assert not bad, "\n".join(f"{d!r} -> {t}" for d, t in bad)


FUZZ_SEED = 20261120
FUZZ_DOCS = 20_000
STRUCT_FUZZ_SEED = 20261121
STRUCT_FUZZ_DOCS = 20_000
_FUZZ_FRAGMENTS = [
    # 圖片語法
    "![a](https://evil.example/i)", "![a][r]", "![a][]", "![r]", "[r]: https://evil.example/d",
    "![", "](", "]", "[", ")", "(", "![a](<https://evil.example/x>)", '![a](u "t")',
    # HTML
    "<img src=https://evil.example/h>", "<IMG\nsrc=x>", "<img/src=x>", "<image src=x>",
    "<picture><source srcset=x></picture>", "<video poster=x>", "<img", ">", '"', "'",
    "<div>", "</div>", "<!--", "-->", "<pre>", "</pre>", "<a title=\"", "<span>", "<details>",
    # 反引號與 fence
    "`", "``", "```", "````", "```rust", "~~~", "~~~ x", " ```", "  ```", "   ```", "    ```",
    "\t```", " ~~~", "```x`y",
    # 空行與各種「像空白」的字元
    "\n", "\n", "\n", "\r\n", "\r", "\n\n", IDSP, NBSP, "\t", " ", "\f", "\v", "\x85",
    LSEP, PSEP, "\x1c", BOM,
    # 容器
    "- ", "* ", "1. ", "> ", ">", "  ",
    # 其他會影響 inline 解析的東西
    "|---|---|", "| a | b |", "|", "$", "\\", "\\`", "\\!", "\\[", "&lt;", "&#96;",
    # 一般文字
    "word", "vec![1, 2]", "x", "中文", " ",
]


def _fragment_docs(seed: int, count: int):
    """片段 fuzzer：把上面的片段隨機相接。"""
    import random

    rng = random.Random(seed)
    eols = ["\n", "\r\n", "\r", "\n\n"]
    for k in range(count):
        if k % 2 == 0:  # 片段直接相接
            yield "".join(rng.choice(_FUZZ_FRAGMENTS) for _ in range(rng.randint(1, 16)))
        else:  # 以行為單位：每行 1–6 個片段，接隨機行尾（讓 fence／HTML block／空行更常出現在行首）
            yield "".join("".join(rng.choice(_FUZZ_FRAGMENTS) for _ in range(rng.randint(1, 6)))
                          + rng.choice(eols) for _ in range(rng.randint(1, 8)))


# --- 結構化 fuzzer（fix round 4）
#
# 片段 fuzzer 很少碰巧產生「容器前綴連續好幾行」的文件，第三輪 review 的兩個繞過（引言裡的表格、
# 註腳定義裡的 HTML block）就是這樣漏掉的。這個 fuzzer 以行為單位，讓同一個容器前綴跨行延續，
# 行的內容從表格、各型 HTML block 開頭／結尾、各種縮排／長度／info string 的 fence、行內 code
# 包住的 payload 裡挑；另外有一種「看起來乾淨」的文件專門打豁免路徑（頂層 fence 配各種收尾）。
_EVIL = "https://evil.example/s"
_S_PAYLOADS = [
    f"![a]({_EVIL})", f'![a](<{_EVIL}> "t")', f"**![a]({_EVIL})**", f"[![a]({_EVIL})](https://l.example)",
    "![a][r1]", "![r1]", "![r1][]", "![ R1 ]",
    f"<img src={_EVIL}>", f"<IMG SRC='{_EVIL}'/>", f"<img\tsrc={_EVIL}>", f"<img/src={_EVIL}>",
    f"<picture><source srcset={_EVIL}>", f"<video poster={_EVIL}></video>", f"<audio src={_EVIL}>",
    f"<svg><image href={_EVIL}/></svg>", f"<image src={_EVIL}>", f"<input type=image src={_EVIL}>",
    f"<embed src={_EVIL}>", f"<object data={_EVIL}></object>", f"<iframe src={_EVIL}></iframe>",
    "<img", "<source", "![", "!", "[", "]", "(", ")",
]
# (第一行的前綴, 之後各行的前綴)
_S_CONTAINERS = [
    ("> ", "> "), (">", ">"), ("> > ", "> > "), ("- ", "  "), ("* ", "  "), ("+ ", "  "),
    ("1. ", "   "), ("2) ", "   "), ("- [ ] ", "  "), ("[^1]: ", "    "), ("[^1]:", "    "),
    ("* > ", "  > "), ("> - ", ">   "), ("> 1. ", ">    "), ("   ", "   "), ("    ", "    "), ("\t", "\t"),
    ("", ""), ("", ""), ("", ""),
]
_S_HTML = [
    # CommonMark HTML block 開頭 type 1–7，與各自的結束記號
    "<pre>", "<script>", "<style>", "<textarea>", "<!--", "<?php", "<!DOCTYPE x", "<!X", "<![CDATA[",
    "<div>", "<details>", "<summary>", "<table>", "<tr><td>", "<p>", "<x-y>", "</div>", '<a href="x">',
    "</pre>", "</script>", "</style>", "</textarea>", "-->", "?>", "]]>", "</details>", "</td></tr></table>",
]
_S_FENCE_INDENT = ["", "", "", " ", "  ", "   ", "    ", "\t"]
_S_FENCE_RUN = ["```", "```", "````", "`````", "~~~", "~~~", "~~~~"]
_S_FENCE_INFO = [
    "", "", "", "rust", "tsx", "c++", "text", " js", "js x", "a`b", "{.x}", "x" * 21, "~~~", "```",
    "mermaid", "Mermaid", " MERMAID", "mermaid x", "geojson", "topojson", "stl", "math", "  \t",
    # cmark 會解開 info string 裡的 entity 與反斜線跳脫
    "&#109;ermaid", "m&#x65;rmaid", "mer\\maid", "\\mermaid", "x-mermaid", "mermaid,x", "{mermaid}", "\tmath",
]
_S_LINES = [
    "| `a | {P} | b` |", "|---|---|---|", "|-|-|-|", "| --- | :-: |", "| a | b |", "`x|{P}|y`", "-|-",
    "`{P}`", "``{P}``", "``` {P} ```", "w `a` `{P}` b", "`a` {P}", "{P}", "<summary>`{P}`</summary>",
    '[r1]: <https://evil.example/d> "ti`tle"', "[r1]:", "===", "---", "***", "# h", "text", "vec![1]",
    'A@{ img: "https://evil.example/m" }', "$`{P}`$", "https://e.example/d```{P}```", "\\`{P}`",
]
_S_NOISE = ["`", "``", "```", "\\", "\\`", "&#96;", "&lt;", "|", "$", "~", "<!-- ` -->", "<b>", "x",
            IDSP, NBSP, "\f", "\v", "\x85", LSEP, PSEP, "\x1c", "\x1d", "\x1e", BOM, "\x00", "\x1b", " ", "\t"]
_S_EOLS = ["\n", "\n", "\n", "\n", "\r\n", "\r", "  \n", "\\\n", "\n\n", "\n\n", "\n \n", "\n\t\n",
           "\n" + IDSP + "\n"]


def _structured_line(rng) -> str:
    kind = rng.random()
    if kind < 0.45:
        return rng.choice(_S_LINES).replace("{P}", rng.choice(_S_PAYLOADS))
    if kind < 0.65:
        return rng.choice(_S_HTML) + (rng.choice(_S_PAYLOADS) if rng.random() < 0.3 else "")
    if kind < 0.9:
        return rng.choice(_S_FENCE_INDENT) + rng.choice(_S_FENCE_RUN) + rng.choice(_S_FENCE_INFO)
    return ""


def _fence_soup_doc(rng) -> str:
    """幾乎都是行首 fence 與 payload 的短文件，偶爾插一行會讓開關錯開的東西（縮排的 fence、HTML block 開頭…）。

    豁免規則裡「有疑慮就整份不豁免」那一半要靠這種文件才測得到：拿掉任何一條疑慮規則，這裡都要有文件失敗。
    """
    fence = rng.choice(["```", "```", "```", "~~~", "````"])
    trouble = [
        " " + fence, "  " + fence, "   " + fence, "    " + fence, "\t" + fence, "> " + fence, ">" + fence,
        "- " + fence, "* " + fence, "1. " + fence, "2) " + fence, "[^1]: " + fence, "[^1]:\n    " + fence,
        "<div>", "<pre>", "<!--", "<script>", "<style>", "<textarea>", "<?", "<!X", "<![CDATA[", "<details>",
        "<x-y>", "</div>", "<table>", "-->", "</pre>", fence + "x y", fence + "mermaid", fence + fence[0],
        fence[:-1], "```", "~~~", "> x", "- x", "    x", "x" + fence, "x[^1]", "", "text", "|---|---|",
        BOM + fence, "\x1c" + fence, LSEP + fence, NBSP + fence,
    ]
    payload = lambda: rng.choice(_S_PAYLOADS[:21])  # noqa: E731 — 只取完整的圖片／標籤
    lines = [rng.choice(trouble)] if rng.random() < 0.7 else []
    for _ in range(rng.randint(1, 3)):
        lines += [fence + rng.choice(["", "", "rust"]), payload(), fence]
        if rng.random() < 0.5:
            lines.append(payload())
    lines = [rng.choice(trouble) if rng.random() < 0.12 else line for line in lines]
    return "".join(line + rng.choice(["\n", "\n", "\n", "\n", "\r\n", "\r", "\n\n"]) for line in lines)


def _clean_looking_doc(rng) -> str:
    """散文＋頂層 fence，專打豁免路徑：fence 內放 payload，收尾有各種對／不對的變化。"""
    if rng.random() < 0.5:
        return _fence_soup_doc(rng)
    out = []
    for _ in range(rng.randint(1, 4)):
        if rng.random() < 0.6:
            out.append(rng.choice(["說明文字", "## 標題", "**粗體** 開頭", "text " + rng.choice(_S_PAYLOADS),
                                   "| a | b |", "|---|---|", "===", "---"]) + rng.choice(_S_EOLS))
        run = rng.choice(_S_FENCE_RUN)
        info = rng.choice(_S_FENCE_INFO) if rng.random() < 0.5 else rng.choice(["", "rust", "tsx"])
        out.append(run + info + rng.choice(["\n", "\n", "\r\n", "\r"]))
        for _ in range(rng.randint(0, 4)):
            body = rng.choice([
                rng.choice(_S_PAYLOADS), "  " + rng.choice(_S_PAYLOADS), "- " + rng.choice(_S_PAYLOADS),
                "> x", "", "<div>", "<!--", "```", "~~~", " " + run, "   " + run + run[0], "    " + run,
                "\t" + run, run + " x", run[:-1], rng.choice(_S_NOISE) + run, "vec![1]",
            ])
            out.append(body + rng.choice(_S_EOLS))
        closer = rng.random()
        if closer < 0.6:
            out.append(run + rng.choice(["", "", " ", "\t ", run[0]]) + rng.choice(["\n", "\n", "\r\n", "\r", ""]))
        elif closer < 0.85:
            out.append(rng.choice([" ", "  ", "   ", "    ", "\t", rng.choice(_S_NOISE)]) + run + "\n")
        out.append(rng.choice(["", "", rng.choice(_S_PAYLOADS) + "\n", "`" + rng.choice(_S_PAYLOADS) + "`\n"]))
        if rng.random() < 0.1:  # 偶爾混進一行會讓整份失去豁免的東西
            out.append(rng.choice(_S_CONTAINERS)[0] + rng.choice(_S_HTML + ["x"]) + "\n")
    return "".join(out)


def _structured_docs(seed: int, count: int):
    import random

    rng = random.Random(seed)
    for _ in range(count):
        if rng.random() < 0.4:
            yield _clean_looking_doc(rng)
            continue
        first, cont = rng.choice(_S_CONTAINERS)
        lines = []
        for k in range(rng.randint(1, 8)):
            if k == 0:
                prefix = first
            else:  # 多數時候延續同一個容器，偶爾換容器或掉出去（lazy continuation）
                prefix = cont if rng.random() < 0.8 else rng.choice(_S_CONTAINERS)[0]
            line = _structured_line(rng)
            if rng.random() < 0.15:
                line = rng.choice(_S_NOISE) + line
            lines.append(prefix + line + rng.choice(_S_EOLS))
        if rng.random() < 0.5:
            lines.insert(0, "x[^1]\n\n")  # 讓 [^1]: 成為真的註腳定義
        yield "".join(lines)


def _fuzz_failures(docs, limit: int = 5) -> list:
    """回傳 [(序號, 文件, 輸出, 命中)]；給 CI 的兩個 fuzz 測試與本機的大量 fuzz 共用。"""
    neutralize = _neutralizer_module()["neutralize"]
    failures = []
    for i, doc in enumerate(docs):
        out = neutralize(doc)
        tags = _rendered_fetching_tags(out)
        if tags:
            failures.append((i, doc, out, tags))
            if len(failures) >= limit:
                break
    return failures


@pytest.mark.parametrize(
    "name, seed, count, gen",
    [("fragment", FUZZ_SEED, FUZZ_DOCS, _fragment_docs),
     ("structured", STRUCT_FUZZ_SEED, STRUCT_FUZZ_DOCS, _structured_docs)],
)
def test_differential_fuzz_no_image_ever_renders(name, seed, count, gen):
    failures = _fuzz_failures(gen(seed, count))
    assert not failures, (
        f"{name} fuzzer seed={seed} 失敗 {len(failures)} 件，第一件：#{failures[0][0]} "
        f"doc={failures[0][1]!r} out={failures[0][2]!r} tags={failures[0][3]}"
    )


def test_fuzz_corpora_actually_contain_images_and_exempt_fences():
    """語料要打得到東西：未處理時多數會渲染出圖片，而且有一部分文件真的走到豁免路徑。"""
    neutralize = _neutralizer_module()["neutralize"]
    for gen, seed in ((_fragment_docs, FUZZ_SEED), (_structured_docs, STRUCT_FUZZ_SEED)):
        docs = list(gen(seed, 2_000))
        assert sum(bool(_rendered_fetching_tags(d)) for d in docs) > 500
    # 輸出裡還有原樣的 `![` 或圖片類標籤，只可能是在被豁免的 fence 裡
    exempted = sum(
        1 for out in map(neutralize, _structured_docs(STRUCT_FUZZ_SEED, 2_000))
        if "![" in out or _HTML_IMG.search(out)
    )
    assert exempted > 20, f"結構化語料裡只有 {exempted} 份保留了 fence 內的 payload，豁免路徑沒被測到"


# ------------------------------------------------------- Claude 失敗時 fail closed

FAILCLOSED_STEP = "Claude 步驟失敗時 fail closed"
GATE_STEP = "閘門判定"
FAILED_MARKER = "<!-- gs-auto-fix:ai-review-incomplete -->"


def test_failclosed_labels_comments_and_removes_output(tmp_path):
    (tmp_path / "review-output.md").write_text("半成品\n", encoding="utf-8")
    (tmp_path / "comment.md").write_text("舊留言\n", encoding="utf-8")
    res = _run(_step_script(FAILCLOSED_STEP), tmp_path, _fake_gh(tmp_path), {})
    assert res.returncode == 0, res.stderr
    calls = _calls(tmp_path)
    assert "pr edit 1 --add-label needs-work" in calls
    assert "pr comment 1 --body-file review-failed.md" in calls
    body = (tmp_path / "review-failed.md").read_text(encoding="utf-8")
    assert "不是對程式碼的判定" in body and FAILED_MARKER in body
    assert not (tmp_path / "review-output.md").exists()
    assert not (tmp_path / "comment.md").exists()


def _existing_comments(tmp_path: Path, *comments: tuple[str, str]):
    """PR 上既有的留言：(作者 login, 內容)。內容裡的換行以空格代替（假 gh 一行一則）。"""
    (tmp_path / "gh-comments.txt").write_text(
        "".join(f"{login}\t{body}\n" for login, body in comments), encoding="utf-8", newline="\n"
    )


def _run_failclosed_path(tmp_path: Path, step: str):
    """兩條會貼「AI review 沒有完成」的路徑：Claude 步驟失敗、貼留言 step 內的 fail_closed（用過大輸出觸發）。"""
    if step == VERDICT_STEP:
        (tmp_path / "review-output.md").write_text("a" * 300_000 + "\nNEEDS_WORK: no\n", encoding="utf-8")
    return _run(_step_script(step), tmp_path, _fake_gh(tmp_path), {})


@pytest.mark.parametrize("step", [FAILCLOSED_STEP, VERDICT_STEP])
@pytest.mark.parametrize("login", ["github-actions", "github-actions[bot]"])
def test_failclosed_does_not_repeat_comment(tmp_path, step, login):
    _existing_comments(tmp_path, ("someone", "舊的"), (login, f"{FAILED_MARKER} AI review 沒有完成"))
    res = _run_failclosed_path(tmp_path, step)
    assert (res.returncode == 0) is (step == FAILCLOSED_STEP), res.stderr
    calls = _calls(tmp_path)
    assert "pr edit 1 --add-label needs-work" in calls, "label 一定要加"
    assert "pr comment" not in calls, "已經有同樣的留言就不要再貼"


@pytest.mark.parametrize("step", [FAILCLOSED_STEP, VERDICT_STEP])
def test_forged_marker_from_non_bot_does_not_suppress_comment(tmp_path, step):
    """任何人都能在 PR 留言貼上隱藏標記；只有 Actions bot 自己的留言才算數。"""
    _existing_comments(tmp_path, ("attacker", FAILED_MARKER), ("github-actions-evil", FAILED_MARKER))
    _run_failclosed_path(tmp_path, step)
    calls = _calls(tmp_path)
    assert "pr edit 1 --add-label needs-work" in calls
    assert "pr comment 1 --body-file review-failed.md" in calls, "非 bot 貼的標記不能讓失敗留言消失"
    assert calls.index("pr edit 1 --add-label needs-work") < calls.index("pr view"), "先加 label"


def test_failclosed_label_added_even_if_comment_query_fails(tmp_path):
    (tmp_path / "gh-view-fail").write_text("", encoding="utf-8")
    res = _run(_step_script(FAILCLOSED_STEP), tmp_path, _fake_gh(tmp_path), {})
    calls = _calls(tmp_path)
    assert calls.index("pr edit 1 --add-label needs-work") < calls.index("pr view"), "先加 label"
    assert "pr comment 1 --body-file review-failed.md" in calls, "查不到既有留言時寧可重貼"
    assert res.returncode == 0, res.stderr


def test_leak_check_pattern_only_wording_is_precautionary(tmp_path):
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJl"
    _leak_check(tmp_path, f"文件範例 {jwt}\nNEEDS_WORK: no\n", {})
    body = (tmp_path / "leak-blocked.md").read_text(encoding="utf-8")
    assert "範例" in body or "測試" in body
    assert "輪換" not in body, "只是樣式命中時不要叫管理者輪換 token"


def test_leak_check_literal_value_wording_says_rotate(tmp_path):
    _leak_check(tmp_path, f"值 {FAKE_SECRET}\nNEEDS_WORK: no\n", {"LEAK_CLAUDE_TOKEN": FAKE_SECRET})
    body = (tmp_path / "leak-blocked.md").read_text(encoding="utf-8")
    assert "輪換" in body


def test_failclosed_runs_only_when_claude_step_failed():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["review"]["steps"]
    names = [s.get("name") for s in steps]
    claude = next(s for s in steps if s.get("uses", "").startswith("anthropics/claude-code-action"))
    assert claude.get("id") == "claude"
    fc = next(s for s in steps if s.get("name") == FAILCLOSED_STEP)
    assert "failure()" in fc["if"] and "steps.claude.outcome == 'failure'" in fc["if"]
    assert names.index(FAILCLOSED_STEP) < names.index(GATE_STEP)


def _gate(tmp_path: Path, gh_body: str):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "gh").write_text("#!/usr/bin/env bash\n" + gh_body + "\n", encoding="utf-8", newline="\n")
    (bindir / "gh").chmod(0o755)
    return _run(_step_script(GATE_STEP), tmp_path, bindir, {})


@pytest.mark.parametrize(
    "gh_body, blocked, why",
    [
        ("printf 'bug\\nneeds-work\\n'", True, "有 needs-work → 紅燈"),
        ("printf 'bug\\n'", False, "沒有 needs-work → 綠燈"),
        ("echo 'HTTP 502' >&2; exit 1", True, "gh pr view 失敗不能放行"),
    ],
)
def test_gate(tmp_path, gh_body, blocked, why):
    res = _gate(tmp_path, gh_body)
    assert (res.returncode != 0) is blocked, f"{why}: {res.stdout} {res.stderr}"


# ------------------------------------------------------- 靜態安全不變式


def _claude_step() -> dict:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["review"]["steps"]:
        if step.get("uses", "").startswith("anthropics/claude-code-action"):
            return step
    raise AssertionError("找不到 claude-code-action step")


def _flag_values(args: str, flag: str) -> list[str]:
    """抽出 claude_args 裡某個 flag 後面、到下一個 --flag 之前的所有值（與 action 的解析方式一致）。"""
    import shlex

    tokens = shlex.split(args)
    out: list[str] = []
    for i, tok in enumerate(tokens):
        if tok == flag:
            for nxt in tokens[i + 1 :]:
                if nxt.startswith("--"):
                    break
                out.append(nxt)
    return out


def test_model_has_no_shell_access():
    """模型拿到 Bash 就等於 diff 裡的注入文字可以呼叫 gh 或 `cat /proc/self/environ`。

    `Bash(gh pr comment:*)` 之類的白名單是指令前綴比對，不是參數限制：
    `gh pr comment $PR --body "$(env)"` 會通過比對，而 $( ) 在 shell 展開，
    等於把特權 context 的 token 貼成公開留言。

    而且光是「allowedTools 不列 Bash」不夠：--allowedTools 只是免詢問清單，Bash 仍可用，
    內建唯讀指令（cat 等）免詢問直接跑。所以要 --tools 限定可用工具、--disallowedTools 移除 Bash。
    """
    args = _claude_step()["with"]["claude_args"]
    allowed = _flag_values(args, "--allowedTools") + _flag_values(args, "--allowed-tools")
    assert not any("Bash" in a for a in allowed), f"不得 allow 任何 Bash: {allowed}"
    tools = ",".join(_flag_values(args, "--tools")).split(",")
    assert sorted(tools) == ["Glob", "Grep", "Read", "Write"], f"可用工具只能是讀檔＋Write: {tools}"
    denied = _flag_values(args, "--disallowedTools")
    for name in ("Bash", "Monitor", "WebFetch", "mcp__*"):
        assert name in denied, f"{name} 必須以裸名 deny（從 context 移除）"


def test_model_file_tools_are_path_scoped():
    """寫入只放行 review-output.md；.git、/proc、runner temp 一律 deny；讀寫限在工作目錄。"""
    step = _claude_step()
    args = step["with"]["claude_args"]
    allowed = _flag_values(args, "--allowedTools")
    assert allowed == ["Edit(/review-output.md)"], f"唯一的 allow 只能是寫 review-output.md: {allowed}"
    for bare in ("Write", "Read", "Edit"):
        assert bare not in allowed, f"裸的 {bare} allow 會放行工作目錄外的路徑"
    denied = _flag_values(args, "--disallowedTools")
    for rule in ("Read(/.git/**)", "Read(//proc/**)", "Read(//etc/**)",
                 "Read(/${{ runner.temp }}/**)", "Edit(/${{ runner.temp }}/**)"):
        assert rule in denied, f"缺少 deny rule {rule}"
    assert "--strict-mcp-config" in args
    assert _flag_values(args, "--setting-sources") == ["user"]
    settings = __import__("json").loads(step["with"]["settings"])["permissions"]
    assert settings["blockReadsOutsideWorkingDirectories"] is True
    assert settings["blockWritesOutsideWorkingDirectories"] is True


def test_checkout_does_not_persist_credentials():
    """GITHUB_TOKEN 不能留在模型讀得到的工作目錄 git 設定裡。"""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    checkouts = [s for s in doc["jobs"]["review"]["steps"]
                 if s.get("uses", "").startswith("actions/checkout")]
    assert checkouts, "找不到 checkout"
    for s in checkouts:
        assert (s.get("with") or {}).get("persist-credentials") is False, s


def test_gh_token_not_at_job_level():
    """job 層級 env 會被每一步（含模型那步）繼承；GH_TOKEN 只給要呼叫 gh 的 step。"""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "GH_TOKEN" not in (doc["jobs"]["review"].get("env") or {})
    import re

    for s in doc["jobs"]["review"]["steps"]:
        # 真的呼叫 gh 的行（行首或 $( ) 裡），不算 echo 訊息裡提到的 gh
        if re.search(r"(^\s*|\$\()gh\s", s.get("run") or "", re.M):
            assert "GH_TOKEN" in (s.get("env") or {}), f"{s.get('name')} 呼叫 gh 卻沒有 GH_TOKEN"


def test_comment_is_posted_via_body_file():
    """--body-file 不經 shell 展開；改用 --body \"$(...)\" 會重新打開注入路徑。"""
    script = _step_script(VERDICT_STEP)
    assert "--body-file" in script
    assert '--body "' not in script


# ------------------------------------------------------- Genesis Deck 混合初審

GDECK_STEP = "Genesis Deck 初審（地端）"


def _precheck_env(tmp_path: Path, token: str, gdeck_on: str, gdeck_key: str) -> dict[str, str]:
    out = tmp_path / "gh-output.txt"
    out.write_text("", encoding="utf-8")
    res = _run(
        _step_script(PRECHECK_STEP), tmp_path, _fake_gh(tmp_path),
        {"GITHUB_OUTPUT": out.as_posix(), "PR_AUTHOR": "gsinvest017-ai", "PR_AUTHOR_TYPE": "User",
         "TOKEN": token, "GDECK_ON": gdeck_on, "GDECK_KEY": gdeck_key},
    )
    assert res.returncode == 0, res.stderr
    return dict(l.split("=", 1) for l in out.read_text(encoding="utf-8").splitlines() if l)


@pytest.mark.parametrize(
    "token, on, key, expect",
    [
        ("", "true", "k", {"skip": "false", "gdeck": "true", "claude": "false"}),
        ("tok", "true", "", {"skip": "false", "gdeck": "false", "claude": "true"}),
        ("tok", "false", "k", {"skip": "false", "gdeck": "false", "claude": "true"}),
        ("", "true", "", {"skip": "true", "gdeck": "false", "claude": "false"}),
    ],
)
def test_precheck_gdeck_matrix(tmp_path, token, on, key, expect):
    got = _precheck_env(tmp_path, token, on, key)
    assert {k: got.get(k) for k in expect} == expect


def _gdeck_step(tmp_path: Path, files: str, diff: str, max_chars: int = 30000) -> str:
    """跑地端初審 step；只測「不打網路就能決定升級」的分支，回傳寫進 GITHUB_OUTPUT 的內容。"""
    (tmp_path / "pr.diff").write_text(diff, encoding="utf-8")
    (tmp_path / "pr-meta.txt").write_text("標題: t", encoding="utf-8")
    out = tmp_path / "gh-output.txt"
    out.write_text("", encoding="utf-8")
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    paths = doc[True]["workflow_call"]["inputs"]["escalate_paths"]["default"]
    bindir = _fake_gh(tmp_path, files)
    # step 用 python3；Windows 的 Git Bash 不一定找得到，指向跑測試的這個直譯器
    shim = bindir / "python3"
    shim.write_text(f'#!/usr/bin/env bash\nexec "{_msys(Path(sys.executable))}" "$@"\n',
                    encoding="utf-8", newline="\n")
    shim.chmod(0o755)
    res = _run(
        _step_script(GDECK_STEP), tmp_path, bindir,
        {"GITHUB_OUTPUT": out.as_posix(), "GDECK_API_KEY": "k", "GDECK_MODEL": "m", "PR": "1",
         "MAX_CHARS": str(max_chars), "ESCALATE_PATHS": paths, "REPO": "o/r", "PROJECT_CONTEXT": ""},
    )
    assert res.returncode == 0, res.stderr
    assert not (tmp_path / "review-output.md").exists(), "升級時地端不能先寫出 review"
    return out.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "files",
    [".github/workflows/ci.yml", "src/auth/login.py", "app/secrets.py", "Dockerfile", "certs/server.pem"],
)
def test_gdeck_escalates_high_risk_paths(tmp_path, files):
    assert "escalate=true" in _gdeck_step(tmp_path, f"README.md\n{files}", "small diff")


def test_gdeck_escalates_large_diff(tmp_path):
    assert "escalate=true" in _gdeck_step(tmp_path, "src/app.py", "x" * 101, max_chars=100)


def test_claude_step_runs_only_when_needed():
    """沒開地端時照舊跑 Claude；開了的話只在升級（或地端那步沒輸出）時跑。"""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    step = next(s for s in doc["jobs"]["review"]["steps"] if s.get("name") == "Claude review（教學語氣）")
    cond = step["if"]
    assert "steps.precheck.outputs.claude == 'true'" in cond
    assert "steps.gdeck.outputs.escalate != 'false'" in cond   # 空字串（地端壞掉）也要升級
