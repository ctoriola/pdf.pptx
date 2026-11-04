# Backblaze B2 Setup Guide

_PDF to Slides: uploads up to 100 MB on Vercel, no card needed_

Vercel rejects any request larger than 4.5 MB. With Backblaze B2, the browser uploads the PDF straight to B2 using a one-time link from the app, the app converts it, and the finished deck comes back as a download link. PDFs of up to 100 MB work, and large decks no longer need recompressing.

B2 gives you 10 GB free with no card. The app deletes each PDF as soon as it is converted, and the bucket clears everything else after about two days, so you will use a tiny fraction of that.

The code is already in the repository. This guide covers the account and settings only, and takes about 20 minutes. Do the parts in order; each step is one action. If a button label differs slightly, look for the closest match on the same page.

## Write these down as you go

| Value | Where you get it | Example |
|---|---|---|
| Bucket name | Part 2, step 6 | pdf-slides-blessing |
| Endpoint | Part 2, step 12 | s3.us-east-005.backblazeb2.com |
| Master keyID + applicationKey | Part 3, step 6 | used once in Part 4, not stored |
| App keyID | Part 5, step 9 | 005a1b2c3d4e5f60000000001 |
| App applicationKey | Part 5, step 9 | K005abcd... (shown only once) |

## Part 1: Create a free Backblaze account

1. Open **backblaze.com** in your browser.
2. Click **Sign Up**, then choose **B2 Cloud Storage** (not Computer Backup).
3. Enter your email address and a password.
4. When asked for a region, choose **US East**. It is closest to Vercel's Washington servers, which keeps uploads and conversions fast.
5. Click **Sign Up for B2** (or **Create Account**).
6. Open the verification email from Backblaze and click the link in it.
7. Sign back in at **secure.backblaze.com** if you are not taken there automatically.

> No card is needed. Without a card on file Backblaze cannot charge you; at worst, a request over a free daily allowance is refused until the next day.

## Part 2: Create the bucket

1. In the left menu, under B2 Cloud Storage, click **Buckets**.
2. Click **Create a Bucket**.
3. Type a bucket name. It must be unique across all of Backblaze: 6 to 63 characters, letters, numbers and hyphens only.
4. If the name is taken, add something personal, for example **pdf-slides-blessing**.
5. Check that the name is accepted (no red warning).
6. Write the bucket name down.
7. Set **Files in Bucket are** to **Private**.
8. Leave **Default Encryption** on Disable.
9. Leave **Object Lock** on Disable.
10. Click **Create a Bucket**.
11. Find your new bucket on the Buckets page.
12. In its details, find **Endpoint** (it looks like s3.us-east-005.backblazeb2.com) and write it down exactly.

## Part 3: Get the master key (for setup only)

Two bucket settings (browser uploads and auto-delete) can only be set with your master key. You use it once in Part 4. The app itself never gets it.

1. In the left menu, click **Application Keys**.
2. Find the **Master Application Key** section at the top.
3. Click **Generate New Master Application Key**.
4. Read the warning (it replaces any previous master key) and click **Yes!** to confirm.
5. The page shows a **keyID** and an **applicationKey**. The applicationKey is shown only once.
6. Copy both into a temporary note. You will delete the note after Part 4.

## Part 4: Run the bucket setup script

The script sets a CORS rule (so browsers may upload to the bucket through the app's one-time links) and auto-delete rules for the app's folders. It uses only built-in Python, so there is nothing to install apart from Python itself.

1. Press the **Windows key**, type **cmd**, and press Enter to open Command Prompt. (Mac: open **Terminal**.)
2. Type **python --version** and press Enter. (Mac: **python3 --version**.)
3. If you see "Python 3.x", go to step 7.
4. If not: open **python.org/downloads** and click the yellow **Download Python** button.
5. Run the installer. On the first screen, tick **Add python.exe to PATH**, then click **Install Now**.
6. When it finishes, close Command Prompt and open a new one (step 1).
7. In your browser, open the repository on GitHub: **github.com/ctoriola/pdf.pptx**.
8. Click the branch dropdown above the file list and choose **claude/optimistic-edison-qfmlzo** (or main, once this work is merged).
9. Open the folder **scripts**, then the file **b2_setup.py**.
10. Click the **Download raw file** button (the download icon at the top right of the file view).
11. In Command Prompt, type **cd Downloads** and press Enter.
12. Type this, with your bucket name in place of YOUR-BUCKET-NAME, then press Enter: `python b2_setup.py --bucket YOUR-BUCKET-NAME` (Mac: start with python3).
13. At "Master application key ID", paste the master keyID and press Enter.
14. At "Master application key", paste the master applicationKey and press Enter. Nothing appears as you paste; that is normal.
15. Check the result says **Bucket "your-bucket" is ready**.
16. Delete the temporary note with the master key.

> For extra safety you can now generate a new master key (Part 3, steps 1 to 4) without saving it. That cancels the one you just used. You only need a master key again to re-run this script.

## Part 5: Create the app key

This key lets the app read and write files in this one bucket only.

1. Go back to **Application Keys**.
2. Click **Add a New Application Key**.
3. Name of Key: type **pdf-to-slides-app**.
4. Allow access to Bucket(s): choose **your bucket** (not "All").
5. Type of Access: choose **Read and Write**.
6. Leave **Allow List All Bucket Names** unticked.
7. Leave **File name prefix** and **Duration** empty.
8. Click **Create New Key**.
9. Copy the **keyID** and the **applicationKey** straight away. The applicationKey is shown only once; if you lose it, delete this key and create another.

> Treat the applicationKey like a password: it goes into Vercel only. Never commit it to GitHub, email it, or paste it into a chat.

## Part 6: Add the settings to Vercel

1. Open **vercel.com** and select the pdf.pptx project.
2. Click **Settings**, then **Environment Variables**.
3. For each row of the table below: type the **Key**, paste the **Value**, leave all environments ticked, and click **Save**.
4. Mark **B2_APPLICATION_KEY** as **Sensitive** if that option is shown.
5. Check the four keys are spelled exactly as shown. A missing or misspelled key makes the app quietly fall back to 4.4 MB direct uploads.
6. Add **GROQ_API_KEY** too if you haven't already (for Condensed and Detailed slides).
7. Open the **Deployments** tab.
8. On the latest deployment, click the **...** menu, then **Redeploy**, then confirm. New settings only reach new deployments.
9. Wait for the deployment to show **Ready**.

| Key | Value |
|---|---|
| B2_KEY_ID | the app keyID (Part 5) |
| B2_APPLICATION_KEY | the app applicationKey (Part 5) |
| B2_BUCKET | the bucket name (Part 2) |
| B2_ENDPOINT | the endpoint, e.g. s3.us-east-005.backblazeb2.com (Part 2) |

## Part 7: Test it

1. Open your Vercel site.
2. Under the upload box, check the hint says **Up to 100 MB**. If it says 4.4 MB, a B2_ setting is missing: go back to Part 6.
3. Choose a PDF larger than 5 MB (any report with images).
4. Pick **Standard** and click **Convert to slides**.
5. Watch the message count up: "Uploading... 37%".
6. Wait for the deck to download.
7. In Backblaze, open **Buckets**, then **Browse Files** on your bucket.
8. Check there is an **outputs** folder with your deck, and no PDF left in **uploads**.

## Troubleshooting

| What you see | Likely cause and fix |
|---|---|
| Hint says "Up to 4.4 MB" | A B2_ setting is missing or misspelled, or you didn't redeploy (Part 6). |
| "The upload could not reach storage" | The CORS rule is missing. Re-run the script (Part 4). Check the bucket name you typed matches exactly. |
| "The upload was refused by storage" | B2_KEY_ID or B2_APPLICATION_KEY is wrong, or the key isn't allowed on this bucket. Create a new app key (Part 5). |
| "Something went wrong" right after the upload | Open Vercel > project > Logs. Usually the app key is Read Only (it must be Read and Write) or B2_ENDPOINT is wrong. |
| Script says "B2 error (401)" | You used the app key, or a typo. The script needs the master key (Part 3). |
| Script says "No bucket named ..." | The bucket name is misspelled. Copy it from the Buckets page. |
| "python" is not recognized | Python isn't installed or wasn't added to PATH. Reinstall and tick "Add python.exe to PATH" (Part 4, step 5). |
| The page times out on a very long PDF | Vercel stops functions after 300 seconds. Try Standard, or split the PDF. MAX_PAGES (default 80) caps the length. |
