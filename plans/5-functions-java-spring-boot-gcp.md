# Functions 改為 Java Spring Boot 部署 GCP — 規劃與費用估算

## 1. 現況

`functions/` 目前是 **Python 3.12 Firebase Functions（2nd gen）**,只有兩支 callable function:

| Function | 行為 | 權限 |
| --- | --- | --- |
| `delete_account_data` | 遞迴刪除 Firestore `users/{uid}` 及其 subcollection,保留登入帳號 | 需登入,只能刪自己 |
| `delete_account` | 刪雲端資料後,再刪 Firebase Auth 帳號 | 需登入,只能刪自己 |

- Region:`asia-east1`(client 與 server 必須一致)。
- Client 端 `lib/core/auth/auth_service.dart` 以 `FirebaseFunctions.instanceFor(region: 'asia-east1').httpsCallable('...')` 呼叫。
- 部署:`firebase deploy --only functions`(見 `firebase.json`)。

> 重點:Firebase Functions 2nd gen 底層**本來就是 Cloud Run**。改成 Java Spring Boot
> 並不會「省錢」,實際上 JVM 記憶體更高、冷啟動更慢,費用只會持平或略增。
> 若目標純粹是省成本,維持現狀(Python)才是最省的。下面的規劃假設改用 Java 是基於
> **語言統一 / 團隊技術棧**等考量,而非成本。

---

## 2. 目標架構

**重點修正:client 端幾乎不用改寫。** Firebase callable 其實是一套**公開且固定的 HTTP 協定**,
只要 Spring Boot 在 Cloud Run 上**照這個協定實作**,client 就能繼續用 `httpsCallable`,
保留 ID Token 自動注入與 `FirebaseFunctionsException` 錯誤處理。

`cloud_functions: ^6.3.2`(本專案版本)提供 `httpsCallableFromUrl` / `httpsCallableFromUri`,
可把 callable 指向任意 URL(即 Cloud Run 服務網址),因此 client 端**最多只改一行**
(把預設 callable 換成 from-url 版本),不需要改錯誤處理、不需引入 `http`/`dio`。

```
Flutter client (cloud_functions 套件不變)
  └─ httpsCallableFromUri(Uri.parse('https://<cloud-run-url>/delete_account')).call()
        SDK 自動附上 Authorization: Bearer <ID_TOKEN>
        SDK 自動包成 { "data": ... } envelope
            │
            ▼
Cloud Run (asia-east1) — Spring Boot,實作 callable 協定
  ├─ Filter:驗證 Bearer token → FirebaseAuth.verifyIdToken() 取 uid(等同 req.auth.uid)
  ├─ 解析 request body 的 { "data": ... }
  ├─ 回傳 { "result": ... };錯誤回 { "error": { "status", "message" } } + 對應 HTTP code
  ├─ POST /delete_account_data → 刪 Firestore users/{uid}
  └─ POST /delete_account       → 刪 Firestore + FirebaseAuth.deleteUser(uid)
```

### Firebase callable 協定(Spring Boot 要對齊的重點)
- **Request**:`POST`,body = `{"data": <payload>}`(本專案 payload 為空),
  header 帶 `Authorization: Bearer <Firebase ID Token>`(SDK 自動加,登入後即有)。
- **Response 成功**:HTTP 200,body = `{"result": <payload>}`。
- **Response 失敗**:body = `{"error": {"status": "UNAUTHENTICATED", "message": "..."}}`,
  搭配對應 HTTP code(401/403/500…)。`status` 字串對應 client 的 `FirebaseFunctionsException.code`。
- 對齊此協定後,client 端 `catch (FirebaseFunctionsException)` **完全沿用**。

### 與現況唯一的差異
- **Client**:`auth_service.dart` 把 `_functions.httpsCallable('delete_account')` 換成
  `_functions.httpsCallableFromUri(Uri.parse('$_baseUrl/delete_account'))`;
  新增一個 Cloud Run base URL 常數。**僅此而已。**
  > 若連 URL 都想不改,可把 Cloud Run 掛在
  > `https://<region>-<project>.cloudfunctions.net/...` 慣例網址後(用 Hosting rewrite 或
  > 自訂網域),則 client 一行都不用動 — 但通常多此一舉,改一行 from-url 最單純。
- **Firestore `recursive_delete`**:Java Admin SDK **沒有**內建 recursive delete,
  需自行實作遞迴刪除 subcollection(或保留一支極簡 Python/Node function 專門做這件事)。
  ← 這是真正需要額外工的地方,與 client 無關。

---

## 3. 實作步驟

1. **建立 Spring Boot 專案**(`functions-java/`,與現有 `functions/` 並存以利切換)
   - Spring Boot 3.x + Java 21,Maven/Gradle。
   - 依賴:`spring-boot-starter-web`、`com.google.firebase:firebase-admin`。
   - 為降低冷啟動,評估 **Spring Boot + GraalVM Native Image**(見 §5 註)。
2. **驗證 Filter**:`OncePerRequestFilter` 取 `Authorization` header →
   `FirebaseAuth.getInstance().verifyIdToken(token)` → 將 uid 放進 request context;
   缺 token / 驗證失敗回 callable 協定的 `{"error":{"status":"UNAUTHENTICATED",...}}` + 401。
3. **兩支 endpoint(對齊 callable 協定)**:對應原本兩支 function 的邏輯;
   解析 `{"data":...}`、回傳 `{"result":...}`、錯誤回 `{"error":...}`;Firestore 遞迴刪除自行實作。
4. **容器化**:撰寫 `Dockerfile`(distroless / jib 打包),推到 **Artifact Registry**。
5. **部署 Cloud Run**(`asia-east1`):
   - `--memory 512Mi --cpu 1`、`--min-instances 0`(預設,見費用 §4)。
   - 設定 service account,授予 Firestore / Firebase Auth 權限。
6. **Flutter client 微調**(`auth_service.dart`):
   - 僅把 `httpsCallable('delete_account')` 改為 `httpsCallableFromUri(...)` 指向 Cloud Run URL,
     新增 base URL 常數。**保留 `cloud_functions` 依賴與既有錯誤處理。**
7. **CI/CD**:GitHub Actions 或 Cloud Build,build image → deploy Cloud Run。
8. **切換與清理**:灰度驗證後,刪除舊 Python `functions/` 與 `firebase.json` 的 functions 區塊。

---

## 4. 費用估算(asia-east1 / Tier 1)

Cloud Run 計費三項 + 每月免費額度:

| 項目 | 單價 | 每月免費額度 |
| --- | --- | --- |
| vCPU | $0.000024 / vCPU-秒 | 180,000 vCPU-秒 |
| 記憶體 | $0.0000025 / GiB-秒 | 360,000 GiB-秒 |
| 請求數 | $0.40 / 百萬次 | 200 萬次 |

帳號刪除是**極低頻**操作(每月估數百~數千次)。兩種部署策略:

### 方案 A:`min-instances = 0`(scale-to-zero,推薦)
即使每月 1,000 次呼叫、每次 Java 處理 2 秒、1 vCPU + 512 MiB:
- vCPU:1,000 × 2 = 2,000 vCPU-秒 →**全在免費額度內,$0**
- 記憶體:0.5 GiB × 2,000 秒 = 1,000 GiB-秒 →**$0**
- 請求:1,000 次 →**$0**

**Cloud Run 月費 ≈ $0**。額外固定成本只有:
- **Artifact Registry** 映像檔儲存:Java 映像約 300–500 MB,$0.10/GB/月 → **約 $0.05/月**
- **Cloud Build**:每天 120 分鐘免費,偶爾部署 → **$0**

> 代價:scale-to-zero 時 Spring Boot **冷啟動約 5–15 秒**(JVM)。對「刪除帳號」這種
> 一次性、可接受等待的操作影響不大,但 UX 上建議顯示 loading。

### 方案 B:`min-instances = 1`(常駐,避免冷啟動)
保留一個常駐 instance(1 vCPU + 512 MiB),閒置 CPU 以折扣費率計:
- 閒置 CPU:1 × 2,592,000 秒 × ~$0.0000025 ≈ **$6.5/月**
- 記憶體:0.5 × 2,592,000 秒 × $0.0000025 ≈ **$3.2/月**
- **合計約 $8–10/月**(扣除免費額度後略低)。

### 小結

| 策略 | 月費估計 | 冷啟動 | 適用 |
| --- | --- | --- | --- |
| A. min=0 | **≈ $0**(僅 ~$0.05 映像儲存) | 5–15 秒 | **推薦**,低頻操作 |
| B. min=1 | **≈ $8–10** | 無 | 需要即時回應才考慮 |
| 維持 Python 現狀 | ≈ $0 | 較快(~2–5 秒) | 成本最低 |

**結論:以此 workload,選方案 A,每月費用幾乎為零(僅 Artifact Registry 約 $0.05)。**
真正要付費的情境是選 min=1 常駐(約 $10/月),這通常不必要。

---

## 5. 注意事項與建議

- **成本不是改用 Java 的理由**:Java 在 Cloud Run 上記憶體與冷啟動都比 Python 差,
  費用只會持平或略增。改用 Java 應出於技術棧統一等考量。
- **冷啟動優化**:若在意延遲,可用 **GraalVM Native Image**(冷啟動降到 < 1 秒、
  記憶體更省),但 build 較複雜、Firebase Admin SDK 的 reflection 需額外 native hint 設定。
- **遞迴刪除**:Java Admin SDK 無 `recursive_delete`,務必自行實作或保留一支小型
  Node/Python function 專責,避免漏刪 subcollection 造成孤兒資料。
- **安全**:REST endpoint 必須強制驗證 ID Token,且只允許操作 token 內的 uid
  (等同舊的 `req.auth.uid`),不可信任 client 傳入的 uid。
- **替代方案**:若只是想要 Java 而不想自管 Cloud Run,可考慮維持現有 callable
  協定不動、僅在需要新功能時才引入 Java 服務,降低 client 改動範圍。

---

## 6. 待辦(若決定執行)
- [ ] 建立 `functions-java/` Spring Boot 專案骨架
- [ ] 實作 ID Token 驗證 Filter(回傳對齊 callable 錯誤協定)
- [ ] 實作兩支 endpoint(對齊 callable `{"data"}`/`{"result"}`/`{"error"}` 協定)+ Firestore 遞迴刪除
- [ ] Dockerfile / jib + Artifact Registry
- [ ] Cloud Run 部署腳本(asia-east1, min-instances=0)
- [ ] `auth_service.dart` 改用 `httpsCallableFromUri` 指向 Cloud Run(僅一行 + base URL 常數)
- [ ] CI/CD pipeline
- [ ] 灰度驗證後移除舊 Python functions
