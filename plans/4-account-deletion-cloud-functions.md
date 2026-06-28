# 刪除帳號 / 刪除帳號資料（Cloud Functions）

狀態：**已部署**（2026-06-11，本機 Owner 身分 deploy，兩支函式建立於 asia-east1，artifact 清理政策已設 1 天）。
影響範圍：`functions/`、`firebase.json`、`lib/core/auth/auth_service.dart`、`lib/features/profile/account/`、`lib/l10n/`、`.github/workflows/firebase-functions-deploy.yml`
後端：Firebase Cloud Functions（Python）+ Firebase Admin SDK

## 背景 / 問題

App 商店（Google Play / App Store）要求提供「刪除帳號」與「刪除帳號資料」兩種入口。
原本 client 端 `currentUser.delete()` 會因 Firebase 要求**近期重新登入**而失敗，且無法連帶清雲端資料。

## 結論

改由 Cloud Function 以 Admin SDK 執行：在 `functions/main.py` 提供兩支 callable API（region `asia-east1`），
client 端 `AuthService` 改打這兩支 API；另以 Firebase Hosting 上線兩個瀏覽器可用的刪除頁面，
供 Google Play Console「資料刪除」欄位填寫。

| Function | 行為 | 帳號 |
| --- | --- | --- |
| **`delete_account_data`** | 遞迴刪除 Firestore `users/{uid}` 文件及其所有 subcollection。 | 保留 |
| **`delete_account`** | 先刪上述雲端資料，再 `auth.delete_user(uid)`。 | 刪除 |

- 兩者皆以 callable context 的 `auth.uid` 為準 — 使用者**只能刪自己的**，未登入丟 `UNAUTHENTICATED`。
- region = **`asia-east1`**，必須與 client `FirebaseFunctions.instanceFor(region: ...)`（`auth_service.dart` 的 `_functionsRegion`）一致。
- 資料範圍目前僅 Firestore `users/{uid}`。日後新增 Cloud Storage / 其他 collection 時，集中加在 `_delete_user_data(uid)`。

待辦：runtime SA 權限驗證（實機呼叫兩支 API）、CICD 的 SA 權限驗證（Actions 手動 Run workflow）。

## 步驟

1. 實作兩支 callable API（`functions/main.py`）。
2. Client 串接（`auth_service.dart`、帳戶頁 UI、l10n）。
3. 部署前置（Firestore、Blaze、GCP API、venv、runtime SA 權限）。
4. 設定 CICD 自動部署（`.github/workflows/firebase-functions-deploy.yml`）。
5. 上線刪除網頁並填入 Google Play Console「資料刪除」連結。

## 各步驟說明

### 1. 實作兩支 callable API
見「結論」的表格與要點；程式在 `functions/main.py`，共用的刪資料邏輯集中於 `_delete_user_data(uid)`。

### 2. Client 串接
- `auth_service.dart`：`AuthService` 多收一個 `FirebaseFunctions`；`deleteAccount()` 改打 `delete_account` 後本地 `signOut()`；新增 `deleteAccountData()` 打 `delete_account_data`。
- `signed_in_view.dart`：新增「刪除帳號資料」按鈕 + 確認對話框；例外捕捉改 `FirebaseFunctionsException`。
- l10n 新增 `account_delete_data` / `account_delete_data_confirm` / `account_delete_data_done`（en / zh_TW / zh_CN，其餘語系 fallback 到 en）。**待辦**：補進 Google Sheet。
- 依賴：`cloud_functions: ^6.3.2`。

### 3. 部署前置
1. ~~**Firestore**：Console 啟用 Firestore database~~ ✅ 已開通。
2. ~~**Blaze 方案**~~ ✅ 已升級。
3. ~~**首次部署的 GCP API**~~ ✅ 已由首次本機 deploy 自動開通（cloudfunctions/cloudbuild/artifactregistry/run/eventarc/pubsub/storage）。**注意**：首次開通後第一次 deploy 可能因 service agent（`gcf-admin-robot`）尚未傳播而 404（`generateUploadUrl ... Could not authenticate`），等 1–2 分鐘重試即可，非權限問題。
4. **本機/CI 都要先建 `functions/venv`**：firebase CLI 部署前會用 venv 載入 `main.py` 探測函式，缺 venv 直接報錯（`Missing virtual environment at venv directory`）。Python 版本須與 `firebase.json` 的 `"runtime": "python312"` 一致（**勿用 3.14**，runtime 最高支援 3.13，CLI 抓系統預設 python3 會踩到）：
   ```bash
   cd functions && python3.12 -m venv venv && ./venv/bin/pip install -r requirements.txt
   ```
   CICD workflow 已內建此步驟（setup-python 3.12 + 建 venv 後才 deploy）。venv/ 已在 functions/.gitignore。
5. ⚠️ **Runtime service account 權限（最易漏）**：函式以 Admin SDK 刪 Auth user + Firestore，2nd gen 預設跑在 default compute SA（`<專案編號>-compute@developer.gserviceaccount.com`）。**Google 從 2024 起新專案的 default compute SA 不再自動帶 Editor**，故需確認該 SA 具備：
   - **Firebase Authentication Admin**（`roles/firebaseauth.admin`）→ `auth.delete_user`
   - **Cloud Datastore User**（`roles/datastore.user`）→ 刪 Firestore
   - （或一次給 **Editor**）
   - 權限不足會在**執行期**（非部署期）失敗，易誤判。部署後**兩支都要驗**（踩的權限不同）：先用測試帳號呼叫 `delete_account_data`（只驗 Firestore 權限，帳號還在可重測），再呼叫 `delete_account`（多驗 Auth Admin 權限，帳號會被刪，順帶驗 client 登出流程）。

### 4. CICD（`.github/workflows/firebase-functions-deploy.yml`）
- 觸發：push 到 `master` 且 `functions/**`、`firebase.json`、`.firebaserc` 或本 workflow 變更；另支援手動 `workflow_dispatch`。
- 流程：setup-python 3.12 → 建 `functions/venv` + 裝依賴 → `npx firebase-tools deploy --only functions --non-interactive`；憑證用既有 secret **`FIREBASE_SERVICE_ACCOUNT_SEEK_PLAYER_F724E`**（與 hosting workflow 共用），經 `GOOGLE_APPLICATION_CREDENTIALS` 注入，deploy 後清除。
- **權限注意**：hosting 用的 service account 不一定有 functions 部署權限。若 deploy 失敗，需在 GCP IAM 給該 service account 加上 **Cloud Functions Admin**、**Service Account User**、**Cloud Build Editor**（及首次部署所需的 Artifact Registry 權限）。
- functions 的部署與 App 上架（`release.yml`，推 `v*` tag 觸發）各自獨立，互不影響。

### 5. Google Play Console「資料刪除」連結（網頁已上線）

Play Console → **應用程式內容 → 資料刪除** 填：

| 欄位 | URL |
| --- | --- |
| 帳戶刪除要求網址（必填） | `https://seek-player-f724e.web.app/delete-account.html` |
| 資料刪除要求網址（選填） | `https://seek-player-f724e.web.app/delete-data.html` |

- 頁面在 `public/delete-account.html` / `public/delete-data.html`，共用 `public/deletion.js`（登入：Google popup + Email/密碼 + **手機 OTP**，與 App 支援的三種方式一致；手機登入用 `signInWithPhoneNumber` + 可見 reCAPTCHA，點「使用手機號碼登入」才渲染）與 `deletion.css`；Firebase 設定走 Hosting 保留路徑 `/__/firebase/init.js`（compat SDK 12.14.0，版本跟著 `index.html`）。
- **不可**填 callable function 端點（`cloudfunctions.net/...`）— 那是帶 token 的 POST API，瀏覽器打開只會錯誤；Play 要求已解除安裝者也能在瀏覽器完成刪除。
- `firebase.json` 的 `** → /index.html` rewrite 不影響：實體檔案優先。
- Data safety 表單記得勾「提供刪除機制」。
- 已於 2026-06-11 `firebase deploy --only hosting` 部署，兩頁皆 200。日後改 `public/**` push master 會走 hosting workflow 自動部署。

## 後續

- 重新引入資料儲存層後（見 `impl-decisions.md` Isar 待辦），把新增的雲端資料路徑補進 `_delete_user_data`。
- **Firestore security rules**：兩支刪除 API 走 Admin SDK、**繞過 rules**，故 API 本身不需 rules。但 app 目前無 client 端直連 Firestore（無 `cloud_firestore`），正確狀態是**全拒絕**——若 Console 開通時選了 **test mode**（30 天任何人可讀寫），需立即改成 `allow read, write: if false;`；選 production mode 則免動。日後 client 直讀寫 `users/{uid}` 時再開本人限定規則（`request.auth.uid == uid`），屆時把 `firestore.rules` 收進 repo + `firebase.json`，跟 CICD 一起部署。
- 視需要加 emulator 測試。
