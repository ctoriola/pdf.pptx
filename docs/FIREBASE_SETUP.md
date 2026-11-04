# Firebase Storage Setup Guide

_PDF to Slides: lifting the 4.4 MB upload limit on Vercel_

Vercel rejects any request larger than 4.5 MB. With Firebase Storage, the browser uploads the PDF straight to Google's storage, sends the app only the file's location, and gets the finished deck back as a download link. Uploads of up to 50 MB work, and large decks no longer need recompressing.

The code is already in the repository. This guide covers the accounts and settings only. It takes about 20 minutes. Do the parts in order; each step is one action.

Firebase and Google Cloud rename menu items from time to time. If a label doesn't match exactly, look for the closest equivalent on the same page.

## Write these down as you go

| Value | Where you get it | Example |
|---|---|---|
| Project ID | Part 1, step 6 | pdf-to-slides-4f2a1 |
| Bucket name | Part 3, step 9 | pdf-to-slides-4f2a1.firebasestorage.app |
| apiKey | Part 6, step 6 | AIzaSy... |
| authDomain | Part 6, step 6 | pdf-to-slides-4f2a1.firebaseapp.com |
| appId | Part 6, step 6 | 1:1234567890:web:abc123... |
| Service-account JSON file | Part 7, step 5 | a downloaded .json file |

## Part 1: Create the Firebase project

1. Open **console.firebase.google.com** in your browser.
2. Sign in with the Google account you want to own the project.
3. Click **Create a project** (or **Add project**, or **Create a Firebase project**).
4. Type a project name, for example **pdf-to-slides**.
5. Look under the name field: Firebase shows the **project ID** it will use (for example pdf-to-slides-4f2a1).
6. Write the project ID down. You need it in Part 9.
7. Tick the terms checkbox if asked, then click **Continue**.
8. If asked about AI assistance (Gemini), choose either option. It doesn't affect this app.
9. On the Google Analytics screen, switch Analytics **off**. The app doesn't use it.
10. Click **Create project**.
11. Wait for "Your new project is ready", then click **Continue**.

## Part 2: Switch the project to the Blaze plan

New projects must be on the pay-as-you-go Blaze plan to use Storage. Google's free monthly allowance still applies, and this app deletes every file within a day, so usage stays tiny.

1. In the left sidebar, at the bottom, find the plan label **Spark**.
2. Click **Upgrade** next to it.
3. Choose **Blaze** and click **Select plan** (or **Continue**).
4. Click **Create a Cloud Billing account** if you don't already have one.
5. Choose your country (Nigeria).
6. Enter your card details and billing address.
7. Click **Confirm** or **Start my free trial**, whichever appears.
8. When asked to set a budget, enter a small amount, for example **1 USD**.
9. Click **Continue**, then **Link Cloud Billing account** (or **Purchase**).
10. Check that the sidebar label now says **Blaze**.

> A budget only sends you alert emails; it does not stop spending. For a personal tool with files deleted daily, expect a bill of zero or a few cents.

## Part 3: Turn on Storage

1. In the left sidebar, open **Build** (or **Databases & Storage**).
2. Click **Storage**.
3. Click **Get started**.
4. Choose the bucket location **US-EAST1**. It is close to Vercel's Washington servers and inside Google's no-cost region list (US-CENTRAL1 and US-WEST1 also qualify).
5. Click **Continue**.
6. Choose **Start in production mode**.
7. Click **Create** (or **Done**).
8. Wait for the Files tab to appear.
9. At the top of the Files tab you'll see a path like **gs://pdf-to-slides-4f2a1.firebasestorage.app**.
10. Write down everything after **gs://**. That is your **bucket name**.

> The location cannot be changed later. If you pick the wrong one, create a new project.

## Part 4: Paste the security rules

These rules let a browser upload only a PDF under 50 MB, only into its own folder, and stop anyone reading files through Firebase. The app itself uses the service account (Part 7), which isn't bound by these rules.

1. Still in Storage, click the **Rules** tab.
2. Click inside the editor and select everything (Ctrl+A).
3. Press Delete so the editor is empty.
4. Copy the rules below. They are also in the repository file **storage.rules**.
5. Paste them into the editor (Ctrl+V).
6. Click **Publish**.
7. Check that the "Rules published" message appears.

```
rules_version = '2';

// Firebase Storage rules for PDF to Slides.
// Browsers may only upload a PDF (max 50 MB) into their own uploads/<uid>/ folder.
// Nobody can read, list or overwrite anything through the client SDK; the server uses a
// service account (which bypasses these rules) and hands out time-limited signed links.
service firebase.storage {
  match /b/{bucket}/o {
    match /uploads/{uid}/{fileName} {
      allow create: if request.auth != null
                    && request.auth.uid == uid
                    && fileName.matches('[A-Za-z0-9_-]{8,64}[.]pdf')
                    && request.resource.contentType == 'application/pdf'
                    && request.resource.size < 50 * 1024 * 1024;
    }
    match /{allPaths=**} {
      allow read, write: if false;
    }
  }
}
```

## Part 5: Enable anonymous sign-in

Each visitor gets an invisible, temporary identity. The rules use it to keep each upload in its owner's folder, and the app checks it before touching a file. Visitors never see a login screen.

1. In the left sidebar, open **Build** (or **Security**) and click **Authentication**.
2. Click **Get started**.
3. Open the **Sign-in method** tab.
4. In the list of providers, click **Anonymous**.
5. Switch the **Enable** toggle on.
6. Click **Save**.
7. Check that Anonymous now shows **Enabled**.

## Part 6: Register the web app and copy its config

1. Click **Project Overview** at the top of the left sidebar.
2. Click the web icon **</>** (if apps already exist: **Add app**, then the web icon).
3. Type an app nickname, for example **pdf-to-slides-web**.
4. Leave **Also set up Firebase Hosting** unticked.
5. Click **Register app**.
6. A code block appears containing **firebaseConfig**. Copy these five values into your notes: **apiKey**, **authDomain**, **projectId**, **storageBucket**, **appId**. Copy only the text inside the quotes.
7. Ignore messagingSenderId and measurementId. The app doesn't need them.
8. Click **Continue to console**.

> These values are public (they end up in every visitor's browser), so they're safe in Vercel settings. To find them again: gear icon > Project settings > General > Your apps.

## Part 7: Create the service-account key

This key lets the app read uploads and create download links. Treat it like a password.

1. Click the gear icon next to Project Overview.
2. Click **Project settings**.
3. Open the **Service accounts** tab.
4. Click **Generate new private key**.
5. Click **Generate key**. A .json file downloads.
6. Move the file somewhere private, outside the project folder.
7. Never commit it to GitHub, email it, or paste it into a chat.

> If the key ever leaks: Google Cloud console > IAM & Admin > Service accounts > the firebase-adminsdk account > Keys > delete it, then generate a new one and update Vercel.

## Part 8: Auto-delete files after one day

The app deletes each uploaded PDF as soon as the deck is built. This rule removes everything else (finished decks, abandoned uploads) after a day, so the bucket never fills up.

1. Open **console.cloud.google.com** (same Google account).
2. In the project picker at the top of the page, select your Firebase project.
3. Open the menu (three lines, top left) and click **Cloud Storage**, then **Buckets**.
4. Click your bucket (the bucket name from Part 3).
5. Open the **Lifecycle** tab.
6. Click **Add a rule**.
7. Under action, choose **Delete object**, then click **Continue**.
8. Under conditions, tick **Age** and enter **1** (day).
9. Click **Continue**, then **Create**.
10. Check that the rule appears in the Lifecycle list.

> Download links expire after 1 hour anyway; this rule removes the files themselves.

## Part 9: Add the settings to Vercel

1. Open **vercel.com** and select the pdf.pptx project.
2. Click **Settings**, then **Environment Variables**.
3. For each row of the table below: type the **Key**, paste the **Value**, leave all environments ticked (Production, Preview, Development), and click **Save**.
4. For FIREBASE_SERVICE_ACCOUNT: open the .json file from Part 7 in Notepad, press Ctrl+A, Ctrl+C, and paste the whole contents as the value. Mark it **Sensitive** if that option is shown.
5. Check that all six FIREBASE_ keys are spelled exactly as shown. A missing or misspelled key makes the app quietly fall back to 4.4 MB direct uploads.
6. Add **GROQ_API_KEY** too if you haven't already (for Condensed and Detailed slides).
7. Open the **Deployments** tab.
8. On the latest deployment, click the **...** menu, then **Redeploy**, then confirm. New settings only reach new deployments.
9. Wait for the deployment to show **Ready**.

| Key | Value |
|---|---|
| FIREBASE_API_KEY | apiKey (Part 6) |
| FIREBASE_AUTH_DOMAIN | authDomain (Part 6) |
| FIREBASE_PROJECT_ID | projectId (Part 6) |
| FIREBASE_STORAGE_BUCKET | storageBucket, e.g. xxx.firebasestorage.app, without gs:// |
| FIREBASE_APP_ID | appId (Part 6) |
| FIREBASE_SERVICE_ACCOUNT | the entire contents of the .json key file (Part 7) |

## Part 10: Test it

1. Open your Vercel site.
2. Under the upload box, check the hint says **Up to 50 MB**. If it says 4.4 MB, a FIREBASE_ setting is missing: go back to Part 9.
3. Choose a PDF larger than 5 MB (any report with images).
4. Pick **Standard** and click **Convert to slides**.
5. Watch the message count up: "Uploading... 37%".
6. Wait for the deck to download.
7. In the Firebase console, open **Storage** > **Files**.
8. Check there is an **outputs** folder with your deck, and that the uploaded PDF is gone from **uploads**.

## Troubleshooting

| What you see | Likely cause and fix |
|---|---|
| Hint says "Up to 4.4 MB" | One of the six FIREBASE_ settings is missing or misspelled, or you didn't redeploy (Part 9). |
| "Could not start an upload session" | Anonymous sign-in is off (Part 5). |
| "The upload was refused" | Rules not published (Part 4), the file isn't a PDF, or it is over 50 MB. |
| "Your session has expired" | FIREBASE_PROJECT_ID doesn't match the project. Copy projectId again. |
| "Something went wrong" right after the upload | Open Vercel > project > Logs. Usually FIREBASE_SERVICE_ACCOUNT was pasted incompletely, or FIREBASE_STORAGE_BUCKET includes gs://. |
| Download link shows AccessDenied or SignatureDoesNotMatch | The key belongs to a different project. Generate a new key in this project (Part 7). |
| The page times out on a very long PDF | Vercel stops functions after 300 seconds. Try Standard, or split the PDF. MAX_PAGES (default 80) caps the length. |
