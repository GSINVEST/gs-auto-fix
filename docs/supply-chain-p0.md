# 供應鏈攻擊預防 P0

來源：`specfeed-test/docs/gdeck-supply-chain-defense-eval`（C10 暫停無人 auto-merge；C02 Action 釘 SHA、zizmor baseline）。

## 1. 暫停無人 auto-merge（C10）

- `.github/workflows/auto-merge.yml` 的 job 條件加上 `vars.AUTO_MERGE_ENABLED == 'true'`。變數未設定即為關閉，**預設不會啟用 native auto-merge**。
- 保留：自動開 PR（`claude-fix*.yml`）、CI、Claude review 都照舊；只是 merge 要由人來按。
- 原因：下游私有 repo 在 GitHub Free 沒有 branch protection，「靠 branch protection 擋 merge」的閘門可能不存在；AI review 也不算獨立的人類批准。
- 重新開啟（P1 試辦時）：在該 repo Settings -> Secrets and variables -> Actions -> Variables 新增 `AUTO_MERGE_ENABLED=true`。前提是該 repo 已有伺服端 branch protection / ruleset（必要 status check，且合併者為人類或受限 SHA），並建議只對非執行文件白名單開放。
- 其他 workflow 已檢查，沒有 `gh pr merge`、`--auto`、`--admin`、merge API 呼叫（僅註解提及）。

## 2. Action 釘 SHA（C02）

- `.github/workflows/*.yml` 的 `uses: owner/repo@tag` 全改為 `@<40 碼 commit SHA>  # <原 tag>`；本地 `./.github/workflows/...` 不改。
- `anthropics/claude-code-action` 原為浮動 `v1`，現釘在當時 v1 指向的 commit，之後靠 Renovate／人工更新。
- 更新方式：
  ```bash
  gh api repos/<owner>/<repo>/git/ref/tags/<tag> --jq '.object.type+" "+.object.sha'
  # type 為 tag（annotated）時再：gh api repos/<owner>/<repo>/git/tags/<sha> --jq .object.sha
  gh api repos/<owner>/<repo>/commits/<sha> --jq .sha   # 確認該 SHA 屬於此 repo
  ```
  更新時連同行尾的 `# <tag>` 註解一起改。

## 3. gitleaks 下載校驗

`secret-scan.yml` 下載 release tarball 後以寫死的 SHA256 驗證（`sha256sum -c`），失敗即中止。更新版本時：

1. 改 `VER`。
2. 從官方 release 取 `gitleaks_<VER>_checksums.txt` 中 `linux_x64` 的雜湊，並自行下載 tarball 以 `sha256sum` 比對確認一致。
3. 更新 `SHA256` 與註解中的取得日期。

## 4. zizmor

```bash
uv tool run zizmor --version          # 本次基準：1.32.0
uv tool run zizmor .github/workflows/
```

baseline 與未處理 findings 見 PR 描述；本 PR 未大改 workflow 去清 findings。後續項目：`persist-credentials: false`、`ci.yml`／`gui-verify.yml` 的最小 `permissions:`、`claude-fix.yml` 的 `issue_comment` 觸發審查、template-injection 資訊級項目。
