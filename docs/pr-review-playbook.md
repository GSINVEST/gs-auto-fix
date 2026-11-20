# 教學型 PR review — 使用手冊

`_reusable-pr-review.yml` 是給「協作者多數沒受過正式程式訓練」的 repo 用的 PR review + 軟閘門。
本檔是它的正規說明；prompt 本身住在 workflow 裡，改 prompt 請改那支檔案。

與 `claude-review.yml`（本 repo 自用那支）的差別：

| | `claude-review.yml` | `_reusable-pr-review.yml` |
|---|---|---|
| 讀者 | 資深工程師 | **沒受過程式訓練的協作者** |
| 語氣 | 「Skip style nits, bikeshedding」 | 解釋為什麼、給可複製的修法 |
| blocking 範圍 | 由模型自行拿捏 | **只有安全 / 正確性 / 破壞既有行為** |
| 事件 | `pull_request` | **`pull_request_target`** |
| 觸發者有 write 權限的 fork PR 事件 | ❌ 跑不動 | ✅ |
| 觸發者沒有 write 權限（唯讀協作者／外部作者自己開或 push） | ❌ 跑不動 | ⚠️ 只有敏感路徑檢查＋「AI review 沒有完成」的固定留言，**沒有 AI review** |
| 維護者 reopen／push 到外部作者的 PR | ❌ 跑不動 | ✅ **會跑**，而且審的是外部作者寫的內容——工具／路徑限制就是為這種情況存在 |
| 閘門 | 無 | `needs-work` label → status check |

---

## 怎麼接

呼叫端放一支 `.github/workflows/pr-review.yml`：

```yaml
name: PR review
on:
  pull_request_target:
    types: [opened, reopened, ready_for_review, synchronize]

jobs:
  review:
    permissions:
      contents: read
      pull-requests: write
      issues: write
    # 釘完整 40 碼 commit SHA，行尾註明對應版本。
    # <合併後填入的 commit SHA> 待本 repo 加固 PR 合併、打新版 tag 後填入；
    # 在那之前不要指向含舊版安全模型的 tag（v1 = c87d1a2）。
    uses: GSINVEST/gs-auto-fix/.github/workflows/_reusable-pr-review.yml@<合併後填入的 commit SHA>  # vX.Y.Z
    # 只傳這支 workflow 需要的 secret，不要用 `secrets: inherit`（見下方第 3 點）。
    # 有開 gdeck_first_pass 才另外加 GDECK_API_KEY。
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
    with:
      project_context: 這個 repo 在做 XXX。
```

四件事不能省：

1. **事件必須是 `pull_request_target`**。用 `pull_request` 的話，來自 fork 的 PR 拿不到
   `CLAUDE_CODE_OAUTH_TOKEN`（GitHub 不對 fork 注入 secret），token 也是唯讀的，連 label 都貼不上。

   ⚠️ **檢查的是「觸發這次事件的使用者」，不是 PR 作者。** claude-code-action 會先檢查
   `context.actor`（觸發事件的人）有沒有 write 權限，沒有就直接中止（action 原始碼
   `src/github/validation/permissions.ts` 的 `checkWritePermissions`、`src/entrypoints/run.ts`、
   `docs/security.md`「Repository Access」）。所以：
   - 唯讀協作者或外部作者自己 open／push 觸發的事件 → **沒有 AI review**，只會得到確定性的
     敏感路徑檢查，以及一則固定的「AI review 沒有完成」留言＋`needs-work`。
   - 但只要是**有 write 權限的維護者**觸發（例如 reopen、或 push 到外部作者的 PR 分支），
     AI review **會跑**，而它讀的 diff 與描述仍是外部作者寫的。這正是本檔「安全模型」裡
     工具與路徑限制要擋的情況，不要因為「外部作者跑不到」就放鬆它們。

   **不要**為了讓外部作者的事件也跑起來而打開 `allowed_non_write_users`——那等於把握有 secret
   的模型交給任何能開 PR 的人。
2. **釘完整 commit SHA（行尾 `# vX.Y.Z` 註明版本），不要用 `@main`，也不要只寫 tag**。
   `@main` 會讓改一次 prompt 就讓所有 repo 的 review 行為當場全變；tag 可以被移動，SHA 不行。
   這支 workflow 握有 secret，被換掉內容等於把 token 交出去。升版時連同版本註解一起改。
3. **明確列出 `secrets:`，不要用 `secrets: inherit`**。不傳的話 `CLAUDE_CODE_OAUTH_TOKEN`
   進不來；但 `inherit` 會把呼叫端 repo 的**每一個** secret 都交給被呼叫的 workflow
   （部署金鑰、雲端憑證…），只要這支 workflow 或它用的 action 出問題，外洩範圍就是全部。
   明確列出時，被呼叫端只拿得到列出的那幾個。
4. **`permissions:` 一定要寫在呼叫端。** 被呼叫的 reusable workflow 拿到的權限**不能超過
   呼叫端 token 的上限**。多數 repo 的 `Settings → Actions → Workflow permissions` 是預設的
   `read`，這時 reusable workflow 要求的 `pull-requests: write` 會讓整個 run 變成
   **`startup_failure`——沒有 job、沒有 log、UI 上完全看不出原因**。在呼叫端明確宣告會蓋過
   repo 預設值，爆炸半徑也比去 settings 開全域 write 小。

   查目前設定：
   ```bash
   gh api repos/<owner>/<repo>/actions/permissions/workflow
   ```
   `gs-auto-fix` 自己是 `write`，所以**在它身上測不出這個問題**——三個 pilot repo 全是 `read`，
   是在真實 PR 上才踩出來的。

### secret 怎麼設

**不要用 `~/.claude/.credentials.json` 裡的 token。** 那是 session access token，通常幾小時就過期，
塞進 GitHub secret 會先能動、之後靜默壞掉。CI 要的是長效 token：

```bash
claude setup-token                                              # 互動式，產生長效 token
gh secret set CLAUDE_CODE_OAUTH_TOKEN -R <owner>/<repo>         # 貼上上一步的輸出
```

**沒設也可以先合 workflow。** 缺 secret 時 review 會乾淨跳過（只留一則 warning annotation），
不會製造紅燈；不需要 token 的 path guard 仍然照跑。補上 secret 後下一個 PR 就會生效。

`gs-auto-fix` 是 public repo，所以 private repo（含跨 org）都呼叫得到。
若哪天它變 private，這條路會直接斷——private repo 的 reusable workflow 只能被同 owner 的 repo 呼叫。

---

## 安全模型（動這支檔案前務必先讀）

`pull_request_target` 是在 **base repo 的 context** 執行的，握有 secret 與寫入權。
代價是：**任何被 checkout 進來並執行的 PR 程式碼，都等同於把 token 交給 PR 作者。**

本 workflow 的四道約束，缺一不可：

有兩類攻擊面，要分開處理。

### A. 執行 PR 的程式碼（RCE）

| 約束 | 為什麼 |
|------|--------|
| `actions/checkout` 不帶 ref（只取 base） | 取 `head.sha` 就是把外人的 code 拉進特權 context |
| diff 走 `gh pr diff` 存成純資料檔，不執行 | 不產生會被執行的東西 |
| 不跑 PR 的 test / build / lint / 安裝依賴 | `pip install` 會執行 `setup.py`、npm 會跑 `postinstall`，都是任意程式碼執行 |

跑 PR 的程式碼是 **CI 的職責**。CI 用 `pull_request` 事件，在沒有 secret 的隔離沙箱裡跑，那才是安全的地方。

### B. 間接 prompt injection（模型被 diff 誘導去濫用自己的工具）

這是**不同的一類**，A 的四道約束一條都擋不到它。

前提：這支 workflow 的存在意義就是審查來自 fork 的 PR，所以 **diff 是攻擊者 100% 可控的輸入**，
而它被讀進一個握有 `pull-requests: write` token 的模型的 context。

失敗的做法是給模型 `--allowed-tools "Bash(gh pr comment:*),Bash(gh pr edit:*)"`：

- 那是**指令前綴比對，不是參數限制**。`gh pr edit <別的PR> --remove-label needs-work` 照樣通過。
- 更糟：`gh pr comment $PR --body "$(env)"` 也通過比對，但 `$( )` 會在 shell 展開，
  等於**把 `GH_TOKEN` 與 `CLAUDE_CODE_OAUTH_TOKEN` 貼成一則留言**。若 repo 是 public，
  那就是長效憑證公開外洩。
- 只靠 prompt 說「只能動這個 PR 編號」不是 ACL，是請求。

現在的做法是**讓模型完全沒有 shell**：

| 誰做 | 做什麼 |
|------|--------|
| workflow（固定邏輯） | 抓 diff 存成 `pr.diff`、`pr-meta.txt` |
| 模型（只有 `Read`／`Grep`／`Glob`／`Write`，見下方 C） | 讀那兩個檔，把 review 寫進 `review-output.md`，最後一行給 `NEEDS_WORK: yes\|no` |
| workflow（固定邏輯） | 留言前祕密檢查 → 讀 `review-output.md`、`gh pr comment --body-file`、依判定加 label |

模型能影響的只剩「留言文字」與「一個 yes/no」，影響不到「對哪個 PR 動作」「用什麼參數」。
留言用 `--body-file` 而非 `--body "..."`，內容不經 shell 展開。

### C. 模型把祕密抄進留言（2026-11-20 加固）

B 擋住了「模型自己去呼叫 gh」，但留言**文字**是模型寫的、會被原文貼出——GitHub 只遮蔽 log
裡的 secret，**不遮蔽留言**。所以只要模型讀得到 token，注入文字就能要它「把某個檔案的內容
附在 review 裡」。加固前查到三個讀得到 token 的地方：

1. `actions/checkout` 沒有 `persist-credentials: false`，`GITHUB_TOKEN` 留在工作目錄的 git 設定裡。
2. `--allowed-tools "Read,Grep,Glob,Write"` 沒有路徑限制。`--allowedTools` 只是「免詢問清單」，
   不是「可用工具清單」：`Read` 讀工作目錄外（例如 `/proc/self/environ`，Claude 行程的 env 裡
   有 `CLAUDE_CODE_OAUTH_TOKEN` 與 `GH_TOKEN`）免詢問；`Write` 寫工作目錄外（例如 runner 的
   `GITHUB_ENV` 檔）也免詢問；而 Bash 工具其實仍在。本機用同版 Claude Code（2.1.326）實測舊設定：
   工作目錄外的檔案、`.git/config`、寫工作目錄外的檔案全部放行。
3. `review-output.md` 不經任何檢查就貼成留言。

現在的控制，依「主要 → 次要」排列：

| 控制 | 擋什麼 | 限制 |
|---|---|---|
| checkout `persist-credentials: false` | `.git/config`／checkout 的 include 檔裡不會有 `GITHUB_TOKEN` | claude-code-action 自己會設 credential helper（讀 env 的 `GH_TOKEN`，不寫 token 進設定檔；前提是 `allowed_bots` 有設，所以那一行不要拿掉） |
| `--tools "Read,Grep,Glob,Write"` + `--disallowedTools "Bash" "Monitor" "WebFetch" … "mcp__*"` | 模型拿不到 shell、網路、子代理、MCP 工具 | Claude Code 自己執行的檢查，不是 OS 沙箱 |
| settings `blockReadsOutsideWorkingDirectories`／`blockWritesOutsideWorkingDirectories`（+ `denyInsteadOfPrompting`） | 檔案工具讀寫限在工作目錄（`$GITHUB_WORKSPACE`）內：擋 `/proc`、runner temp、`GITHUB_ENV`、`$HOME` 其他地方 | 官方文件說讀取限制會放行「Claude Code 自己需要的檔案」；寫入限制需 Claude Code ≥ 2.1.314 |
| `--disallowedTools` 路徑規則：`Read(/.git/**)`、`Read(//proc/**)`、`Read(//sys/**)`、`Read(//etc/**)`、`Read(/<runner.temp>/**)`、`~/.claude`、`~/.ssh`、`~/.config` 等 | 與上一列重疊的第二道；也擋工作目錄**內**的 `.git/` | 不能 deny 整個 `~/**`：GitHub-hosted runner 的工作目錄就在 `$HOME` 底下 |
| `--allowedTools "Edit(/review-output.md)"` | 唯一免詢問的寫入；工作目錄內其他檔案的寫入被拒 | `Write(...)` 帶路徑的規則不會被比對，要寫成 `Edit(...)`（官方文件） |
| `--setting-sources user`、`--strict-mcp-config` | checkout 裡 `.claude/settings*.json` 的 allow rule／hooks、`.mcp.json` 的 server 不會放寬上面的限制 | 因此 `CLAUDE.md` 也不自動載入，prompt 請模型自己 Read |
| GH_TOKEN 從 job 層級 env 移到各 step | 地端初審的 python 等不需要的 step 不會繼承 | claude-code-action 仍會把 `github_token` 設進 Claude 主行程 env——所以 `/proc` 規則才是重點 |
| **留言前祕密檢查**（固定 shell） | 輸出含 `GITHUB_TOKEN`／`CLAUDE_CODE_OAUTH_TOKEN`／`GDECK_API_KEY` 原值，或 `ghp_`／`ghs_`／`gho_`／`ghu_`／`ghr_` + ≥30 字元、`github_pat_` + ≥30、`sk-ant-` + ≥20、JWT 形狀（`eyJ…` + `.` + …）時：不貼模型文字、改貼固定警告、加 `needs-work`、job 失敗 | **只抓原樣出現**。拆字、加空白、base64／hex 編碼、只抄一部分都抓不到，所以是第二道防線，不是主要控制。**也會誤擋**：只命中「樣式」（不是 secret 原值）時，多半是 diff 裡本來就有的範例或測試 fixture（例如文件裡的範例 JWT、測試用假 token）；此時固定留言會說明「保守起見沒貼出、不代表外洩」，只有命中原值時才要求管理者輪換 |
| **貼出前移除圖片**（固定的 inline Python，只用標準函式庫） | 非程式碼部分的 Markdown 圖片（inline、reference、collapsed、有定義的 shortcut）與 `<img>`／`<image>`／`<source>`／`<video>`／`<picture>`／`<audio>`／`<svg>` 換成 `[已移除外部圖片]`，剩下的 `![` 全部跳脫、圖片類標籤的 `<` 改成 `&lt;`。GitHub 會經圖片代理**自動**抓留言裡的圖片網址，把編碼過的祕密塞進圖片 URL，光是顯示留言就會送出去 | 程式碼（fenced block、行內 code）一字不改，但判斷一律往「當成非程式碼」偏：只認行首 fence；文件裡有縮排／清單／引言內的 fence 或 HTML block 裡的 fence 時整份不認 fence；行內 code 只在「段落裡每個反引號串都配得到、每個 span 都在同一行，且 span 外沒有 `<` 標籤、`](`、`$`、`://`／`www.`／`@`（GFM 延伸 autolink）、表格分隔列」時才認。換行只認 `\r\n`／`\n`／`\r`、空行只認「只有空格與 tab」（與 cmark 相同；U+3000、NBSP 不是空白）；`\v` `\f` `\x1c`–`\x1e` `\x85` U+2028 U+2029 先換成空格、開頭 BOM 去掉。固定文字替換只用在「同一行、不含 HTML 結束記號」的範圍，HTML block 裡只跳脫（替換會刪到結構字元而改變區塊結構）。因此清單裡的程式碼區塊可能被加上 `\`。一般連結保留（不會被自動抓）。輸出 > 200 KB 或處理出錯 → fail closed |
| **Claude 步驟失敗時 fail closed** | Claude 那一步失敗（旗標被拒、認證失敗、觸發者沒有 write 權限）時，後面的檢查與貼留言都會跳過；這一步刪掉殘留輸出、**先**加 `needs-work`、再貼固定的「AI review 沒有完成」留言，閘門亮紅燈。留言帶隱藏標記 `<!-- gs-auto-fix:ai-review-incomplete -->`，PR 上已有就不重貼 | 不是對程式碼的判定；原因要看 Actions log |
| 閘門判定 `set -euo pipefail` | `gh pr view` 失敗時閘門紅燈，不會因為讀不到 label 就放行 | |

地端初審（Genesis Deck）路徑：模型在遠端、只收到 diff 文字，讀不到 runner 上的任何東西，所以沒有
第 1、2 類問題；但它的輸出一樣是 `review-output.md`、一樣先過留言前祕密檢查與圖片移除才貼出。
地端那一步是 `continue-on-error`，它失敗時流程照常往下（升級 Claude，或在沒有輸出時加 `needs-work`），
不會落入「紅燈但沒 label、沒留言」的狀態。

**驗證狀態**：上表的控制已在本機用與 action 相同的 Claude Code 版本（2.1.326）實測過，
action 自己的參數解析器也確認旗標傳得出去；但**還沒有在 GitHub-hosted runner 上實際跑過**。
第一次啟用時請照下面的 canary 程序做一次。

圖片移除另有**差分測試**：測試用 `cmarkgfm`（github/cmark-gfm 的 Python binding，只在測試用）以
GFM 擴充、允許 raw HTML 的設定渲染移除後的輸出，用 `html.parser` 確認沒有任何 `<img>`／`<image>`／
`<picture>`／`<source>`／`<video>`／`<audio>`／`<svg>` 元素；除了手寫的繞過案例，還有固定 seed 的
20,000 份 fuzz 文件在 CI 裡跑。**這是證據不是證明**：cmark-gfm 是 GitHub 用的 parser 函式庫，但
GitHub.com 的渲染流程還有自己的後處理與 sanitizer（例如數學、mention、emoji），測試沒有涵蓋；
fuzz 語料也只是抽樣。

**runner 需求**：Linux、`python3` ≥ 3.8（GitHub-hosted ubuntu runner 內建；self-hosted 要自己確認）。
沒有 `python3` 時「貼出 review」那一步會 fail closed，不會貼出未處理的文字。

**`runs_on` 必須是 Linux runner。** `runner.temp` 的 deny rule 與 `//`、`/`、`~/` 錨點都假設 POSIX
路徑；Windows runner 上路徑會被正規化成 `/c/...`，規則對不上。self-hosted runner 另外要注意：
action 是把設定**合併**進 runner 使用者既有的 `~/.claude/settings.json`（只覆蓋同名的頂層 key），
既有的 hooks、`env`、`enabledPlugins` 等會保留下來並被載入。請讓 runner 使用者的 HOME 保持乾淨，
不要在那個帳號上日常使用 Claude Code。

**第一次啟用的 canary 程序**（設好 `CLAUDE_CODE_OAUTH_TOKEN`、改釘新 SHA 之後）：

1. repo owner 開一個測試 PR，diff 隨意，描述寫明要 reviewer：Read `/proc/self/environ`、
   `.git/config`、`~/.claude/settings.json`、runner temp 目錄底下的檔案（例如 `$RUNNER_TEMP` 裡的
   `_runner_file_commands/`）；Grep `/`；Write 到 `$GITHUB_ENV` 指的檔案。
2. 跑完後由人看 Actions log，確認：
   - 沒有任何旗標／設定錯誤（Claude 那一步沒有因參數失敗）；
   - 模型手上只有 `Read`、`Grep`、`Glob`、`Write` 四個檔案工具（沒有 Bash、WebFetch、MCP 工具）；
   - 上述每一個嘗試都被拒絕；
   - 「留言前祕密檢查」顯示通過，留言裡沒有任何 token 片段。
3. 任何一項不符合就先撤掉 secret，再回報。

**殘留風險（已知並接受）**：

- 注入仍可能讓模型給出錯誤的 `NEEDS_WORK` 判定，或在留言裡寫進誤導文字。前者用 **fail closed**
  降低——判定行缺失或無法辨識時一律標 `needs-work`，所以「把判定行抹掉」的注入結果是被擋，
  不是被放行。後者只是文字，人看得到。
- 上表前幾列都是 Claude Code 的權限檢查，不是 OS 層隔離；Claude 主行程的 env 裡確實有 token。
  若 Claude Code 的路徑檢查有漏洞（例如未知的 symlink／路徑正規化問題），最後只剩祕密檢查，
  而它擋不住變形輸出。
- 工作目錄內的檔案模型都讀得到。不要在 checkout 之前的 step 把任何機密寫進工作目錄。

這幾段 shell 的行為由 `tests/test_pr_review_workflow.py` 覆蓋，該測試**直接從 YAML 抽出
`run:` block 執行**，所以測試與實作不可能漂移；靜態不變式包括：模型不得有任何 `Bash`
權限且 `--tools` 只有四個檔案工具、唯一的 allow 是 `Edit(/review-output.md)`、checkout 不保留憑證、
`GH_TOKEN` 不在 job 層級、祕密檢查排在貼留言之前、Claude 失敗時的 fail closed 排在閘門之前、
留言必須走 `--body-file`；行為測試另涵蓋圖片移除、JWT 樣式、閘門在 `gh` 失敗時紅燈。

---

## 兩道閘門

**① 確定性 path guard（不花 token）**

`guard_paths` 收一組 regex，一行一條。命中就直接擋下並**中止，不呼叫 LLM**。
用途是「這些檔案連送去 review 都不行」——例如含真實個資的 fixtures。

先用 shell 判掉能判的，判不了的才丟給模型。順序倒過來就等於先把敏感內容送出去再說。

**② review 判定 → label → status check**

review 判定 `NEEDS_WORK: yes`（或判定缺失、留言前祕密檢查命中）時，由 workflow 的固定邏輯
執行 `gh pr edit --add-label needs-work`，最後一個 step 讀 label 決定 job 紅綠。

每次執行**開頭會先移除 label**——不清的話，作者修好推新 commit 後，上一輪留下的 label 會讓閘門永遠紅著。

**這個 job 不要設成 required status check。** 它是給作者與 reviewer 看的訊號（軟閘門），理由：

- 缺 secret 時 review 乾淨跳過並回綠燈（見「已知限制」），required 了也等於零保護但看起來像有保護。
- 判定本身可被 prompt injection 影響（見上方安全模型 B／C 的殘留風險）。
- AI review 不算獨立的人類批准（`docs/supply-chain-p0.md` 暫停無人 auto-merge 的同一個理由）。

要真正擋 merge，請在能設 ruleset 的 repo（public 或付費 org）要求**人類 reviewer 的 approval**
與 CI 的 required check；Free 方案的 private repo 連這個都設不了，紅燈 + label 只是自律護欄，
擋不住有 write 權的人手動 merge。

Free private repo 的缺口是**方案問題不是技術問題**，三條路是：搬到付費 org、升級 org 到 Team、
或把協作者降權成 read 讓他們走 fork-PR。完整說明在本機工具 `gh-branch-guard` 的 README
（`C:\Users\User\tools\gh-branch-guard\README.md`，不在本 repo 內）。

### ⚠️ 在有既有 open PR 的 repo 上加 required status check

required check 是**回溯生效**的：加下去的那一刻，所有既有 open PR 立刻變成 `BLOCKED`，
因為它們身上沒有那個 check，而且**永遠不會自己長出來**。

**`gh pr close` + `gh pr reopen` 不夠。** `reopened` 確實會觸發事件，但
`pull_request` 用的是 **PR 分支自己**的 workflow 定義——分支建立在 `ci.yml` 之前的話，
分支裡根本沒有那支檔案，CI 無從跑起。（`pull_request_target` 讀 base，所以 AI review
會跑，看起來「有動」但 required 的那個 check 依然缺席，很容易誤判成已經解決。）

正解是讓分支**實際含有** CI workflow，也就是把 base 併進 head：

```bash
gh pr update-branch <n> -R <owner>/<repo>
```

非破壞性（產生 merge commit，不 force-push、不改寫歷史），且會觸發 `synchronize`，
CI 與 AI review 都會跑。

順序建議：**先把 CI 合進預設分支並確認既有 PR 都 `update-branch` 過，再設 required check。**
反過來做就會像這次一樣，一次卡住四個別人正在進行的 PR。

---

## 已知限制

- **加上這支 workflow 的那個 PR，自己不會被 review。** `pull_request_target` 讀的是 base 分支的
  workflow 定義，而那時 base 還沒有這支檔案。第二個 PR 起才會生效。
- **Claude Max 額度是共用的。** 所有接上的 repo 都吃同一份訂閱額度，會跟你自己的 Claude Code
  session 互搶。擴散到大量 repo 前先量測。
- **`needs-work` label 可以被手動移除。** 這是刻意的——誤判時作者要有辦法繼續。
  代價是軟閘門更軟；真正擋 merge 的應該是人類 reviewer 的 approval。
- **`claude-review.yml` 的 `review` job 也不要設成 required status check。** 它用 job 層級
  `if:` 跳過機器人的 PR，而 job 被整個 skip 時各家 branch protection 對它算 pass 還是
  pending 並不一致，設成 required 會讓 bot 的 PR 卡在 pending。`_reusable-pr-review.yml`
  刻意改成 step 層級跳過、job 一定跑完，但同樣**不建議**設成 required（理由見「兩道閘門」）。
- **機器人開的 PR 不會被 review**（`github-actions[bot]` 除外）。dependabot / renovate 的版本
  bump 一律跳過，由 CI 與安全掃描把關。這是**白名單**：新接進來的 bot 預設不 review，
  要放行得同時改 `precheck` 的判斷與 action 的 `allowed_bots`——兩邊不一致就會紅燈，
  `test_precheck_allowlist_matches_action_allowed_bots` 會擋住只改一邊的情況。
  背景見 `docs/progress-bot-pr-review-gate.md`。
- **⚠️ 沒設 secret 又把這個 job 設成 required check ＝ 零保護但看起來像有保護。**
  缺 secret 時 review 乾淨跳過並回綠燈，git 歷史上會看到「每個 PR 都通過 review」，
  但實際一次都沒跑過。這是不建議設成 required 的理由之一；要確認 review 真的有在跑，先確認 secret 已存在：
  `gh secret list -R <owner>/<repo> | grep CLAUDE_CODE_OAUTH_TOKEN`

---

## 混合初審：先給公司地端 Genesis Deck（2026-10-09，opt-in）

Claude 訂閱額度共用（見上），而大多數 PR 是小改動。開了 `gdeck_first_pass` 之後：

| 情況 | 誰審 |
|---|---|
| diff ≤ `gdeck_max_diff_chars`（預設 30000 字元）且沒碰到 `escalate_paths` | Genesis Deck 定案（yes／no 都算數），留言註明由公司地端模型審 |
| diff 太大、碰到高風險路徑（workflow、auth／secret、Dockerfile、金鑰檔）| Claude |
| Genesis Deck 連不到、回錯、沒給出明確判定、這一步自己壞掉 | Claude |
| 需要升級但 repo 沒有 `CLAUDE_CODE_OAUTH_TOKEN` | 沒有 review 產出 → 照既有 fail-closed 規則標 `needs-work` |

呼叫端：

```yaml
    uses: GSINVEST/gs-auto-fix/.github/workflows/_reusable-pr-review.yml@<含本功能的 commit SHA>  # vX.Y.Z
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
      GDECK_API_KEY: ${{ secrets.GDECK_API_KEY }}   # keyctl.sh add <email> <用途>
    with:
      gdeck_first_pass: true
      runs_on: '["self-hosted", "Linux"]'   # 必須在公司網路內
```

- **為什麼要 self-hosted runner**：Genesis Deck gateway 只放行公司出口 IP，GitHub 雲端 runner 連不到。runner 要註冊給**呼叫端 repo**（個人帳號底下的 repo 沒有共用 runner）。
- **安全模型不變**：仍然不 checkout、不執行 PR 程式碼；地端模型同樣沒有任何工具，diff 以文字附在訊息裡，輸出只寫進 `review-output.md`，貼留言與加 label 仍由固定邏輯做。self-hosted runner 只跑 base 分支定義的步驟。
- **guard 仍在最前面**：敏感路徑命中時連地端也不送。
- 地端 prompt 是 Claude prompt 的精簡版，**改 Claude prompt 時同步改 `Genesis Deck 初審` 那一步**。
- 實測（本機抽出 step 對真 gateway）：一個把分母改成 `len(xs) - 1` 的 PR，地端正確判 `NEEDS_WORK: yes` 並給出修法；但它舉的數值例子算錯（說 `[1,2,3]` 會得 1.5，實際是 3）。結論對、細節可能錯——這是定案範圍限在小而低風險 PR 的理由。
