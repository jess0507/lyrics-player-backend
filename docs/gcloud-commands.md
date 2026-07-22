# gcloud commands

## 登入
  1. 還沒登入過這個帳號,要先登入授權:
      ```bash
      gcloud auth login merukoo0507@gmail.com
      ```
  2. 切到別的帳號,要切回來用:
       ```bash
       gcloud config set account merukoo0507@gmail.com
       ```

## 設置Cloud Task 佇列
  1. 先啟用 Cloud Tasks API
      ```bash 
      gcloud services enable cloudtasks.googleapis.com --project=seek-player-f724e
      ```
  2. 設置Cloud Task 佇列 (建議加第2行, 重試設定)
      ```bash
      gcloud tasks queues create lyrics-player-queue --project=seek-player-f724e --location=asia-east1 --max-attempts=5 --max-retry-duration=3600s --max-dispatches-per-second=10
      gcloud tasks queues create generate-lyrics --project=seek-player-f724e --location=asia-east1 --max-attempts=5 --max-retry-duration=3600s --max-dispatches-per-second=10
      ```
  3. 查看佇列
      ```bash
      gcloud tasks queues list --project=seek-player-f724e --location=asia-east1
      ```
  4. 清除佇列
      ```bash
      gcloud tasks queues purge lyrics-player-queue --project=seek-player-f724e --location=asia-east1
      ```
  5. 設定 TASKS_INVOKER_SERVICE_ACCOUNT
  沿用現有的 833102634982-compute@developer.gserviceaccount.com(Function 和兩個 Cloud Run 服務其實都已經在用這顆),不用另外建新帳號。
     - 但要幫它補一個權限——讓它能對自己簽 OIDC token:
     - Function 呼叫 CreateTask(enqueue,現在補的權限管這裡)
        ```bash
        gcloud iam service-accounts add-iam-policy-binding \                                                                                                                                                                                   
            833102634982-compute@developer.gserviceaccount.com \                                                                                                                                                                                 
            --member="serviceAccount:833102634982-compute@developer.gserviceaccount.com" \                                                                                                                                                       
            --role="roles/iam.serviceAccountTokenCreator"                             
        ```
     - Cloud Tasks 之後派工、真的打去 Cloud Run(這是另一件事,權限之前就已經設好了)
  6. Firebase 預設 Storage bucket(seek-player-f724e.firebasestorage.app)套用一條生命週期規則:
  - 內容(aeneas_service/storage-lifecycle.json):凡是路徑前綴為 align/ 的物件,超過 1 天自動刪除。
    ```bash
    gcloud storage buckets update gs://seek-player-f724e.firebasestorage.app \                                                                                                                                                             
        --lifecycle-file=aeneas_service/storage-lifecycle.json
    ```
