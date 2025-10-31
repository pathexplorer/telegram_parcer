### Requirements:
- Google Cloud Platform account with:
    - enabled billing
    - created project
    - created storage bucket
- Telegram API_ID and API_HASH

### Telegram:
- Go to https://my.telegram.org/auth?to=apps
- Enter your phone number for your Telegram account, where you want to get messages. Optional: this account may be joined to the  goals channel. Without this, you get only the first 10-15 symbols from the message
- Enter app title and short name (any names)
- Copy your app api-id and app api-hash

### Code  
- Rename file chats.csv.setup to chats.csv  
- Fill in names of public channels in the format:  
@channel1,@channel2,@channle3  
- Rename file keywords.csv.setup to keywords.csv  
- Simple fill it: word1,word2,word3

### CGP:  
All actions are running in the Cloud Shell  
Set up the variables project and region  
Replace placeholders {your_region}, {your_project_name}  
Run:  
<code>
gcloud config set project {your_project_name}
gcloud config set run/region {your_region}
export PROJECT_ID=$(gcloud config get-value project)
</code>  
Create a special service account  
gcloud iam service-accounts create {name_of_service_account} \
  --display-name="{ant description text}"

Set a variable for the service account  
<code>SERVICE_ACCOUNT="{name_of_service_account}@${PROJECT_ID}.iam.gserviceaccount.com"</code>

#### Create two secrets:
Replace {your telegram API ID} and {your telegram API HASH ID}  
gcloud secrets create telegram_api_id --replication-policy="automatic"
echo -n "{your telegram API ID}" | gcloud secrets versions add telegram_api_id --data-file=-

gcloud secrets create telegram_hash_id --replication-policy="automatic"
echo -n "{your telegram API HASH ID}" | gcloud secrets versions add telegram_hash_id --data-file=-

#### Set up Secret Manager
gcloud secrets add-iam-policy-binding telegram_api_id \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/secretmanager.secretAccessor"

Repeat running the upper code with a new name: telegram_hash_id

#### Create an empty counter file:
echo '{}' > last_checked_ids.json
gsutil cp last_checked_ids.json gs://{your_bucket_name}/telegram_state/last_checked_ids.json

#### Run cloud function:
Replace placeholders {your_region}, {your_project_name}, {your_bucket_name}  
gcloud functions deploy telegramPoller \
   --runtime python311 \
   --trigger-http \
   --entry-point main \
   --region {your_region} \
   --source . \
   --memory 512MB \
   --set-env-vars TELETHON_SESSION_PATH=/tmp/user_session.session,GCP_PROJECT_ID={your_project_name},GCS_BUCKET_NAME={your_bucket_name} \
   --allow-unauthenticated

#### Create and run the Scheduler Job
It runs code every 10 minutes  
Replace placeholders {your_region} in two places, {your_project_name}  
gcloud scheduler jobs create http telegram-poll-job \
  --schedule "*/10 * * * *" \
  --uri https://{your_region}-{your_project_name}.cloudfunctions.net/telegramPoller \
  --http-method GET \
  --location {your-region}