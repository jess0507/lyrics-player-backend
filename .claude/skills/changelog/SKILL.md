---
name: changelog
description: 把已實作但尚未 push 的變更,簡短條列寫進 Changelog.md 的 `## develop` 區塊。使用者輸入 /changelog 時使用。
disable-model-invocation: true
---

# /changelog — 更新 Changelog.md 的 develop 區塊

目標:把「已實作、還沒推上 remote」的項目,用簡短條列補進 repo 根目錄 `Changelog.md` 的 `## develop`。

## 步驟

1. 收集尚未推送的變更(不要 `git fetch`、不要 commit / push):
   - 未推送的 commit:`git log --oneline @{u}..HEAD`(沒有 upstream 時改用 `git log --oneline origin/master..HEAD`)
   - 未提交的變更:`git status --short` 與 `git diff HEAD`(含 staged);untracked 檔案直接讀內容
   - diff 很大時先看 `--stat`,再挑重點檔案讀;README / 文件的變更通常最能說明功能
2. 讀 `Changelog.md`(不存在就依下方格式建立)。
3. 整理成條列並寫入 `## develop`:
   - **只新增、不刪除**:已在 develop 的項目保留;同一件事已有條目就更新那一條,不要重複
   - 不要動 `## develop` 以外的版本區塊
   - 每條一行,以使用者 / API 行為角度描述「做了什麼」,不寫實作細節或檔案清單
   - 前綴標註模組,例如 `whisperx:`、`aeneas:`、`functions:`、`rules:`、`ci:`
   - API 名稱、參數、env 變數用反引號
   - 純重構、測試、格式調整若無行為變化,合併成一條或省略
   - 用繁體中文
4. 完成後列出這次新增 / 更新的條目;沒有未推送變更就直接說明,不改檔。

## 格式

```
# Changelog

## develop
- ...

## 1.1.0(20261120)
- ...
```

版本標題格式為 `## <semver>(<YYYYMMDD>)`,日期為發版日;git tag 只用 `v<semver>`(如 `v1.1.0`)。此指令只維護 develop,除非使用者明確要求才把 develop 搬成新版本區塊。
