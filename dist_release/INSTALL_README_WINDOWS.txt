STDYTIME FOR WINDOWS - QUICK START
==================================

Thank you for installing Stdytime.

This application runs entirely on your local Windows machine.
No cloud account or web hosting is required.

LOCAL BROWSER ACCESS (Simplest)
================================
Just double-click "Stdytime.exe" - it starts the server and opens your browser automatically.
Perfect for single-computer use or testing.

LOCAL-ONLY DEPLOYMENT
=====================
For a true local install, just extract the package and double-click "Stdytime.exe".
The app runs only on this Windows PC and listens on localhost by default.

If you want to create a shortcut, pin the executable, or place it in a Start Menu folder, those are optional Windows conveniences only.

GOOGLE DRIVE DATABASE BACKUP SETUP
----------------------------------
Stdytime uses a LOCAL database for speed.
Google Drive is used as backup/sync storage only.

Important:
- The local database should be the main working database.
- Google Drive should be used as a backup destination.
- Do not place the live working database directly inside a syncing folder.

STEP 1 - INSTALL GOOGLE DRIVE FOR DESKTOP
- Install and sign in to Google Drive for desktop on the Windows machine.
- Make sure your Google Drive folder is available in File Explorer.

STEP 2 - LOCATE YOUR GOOGLE DRIVE PATH
Common examples:
- G:/My Drive/StdyTime/Stdytime.db
- C:/Users/YourName/My Drive/StdyTime/Stdytime.db
- C:/Users/YourName/Google Drive/StdyTime/Stdytime.db

STEP 3 - CONFIGURE THE DATABASE PATHS
In the Stdytime install folder, locate:
- db_config.json.example

Copy it and rename the copy to:
- db_config.json

Edit db_config.json so it looks similar to this:

{
  "db_path": "C:/Users/YourName/AppData/Local/StdyTime/Stdytime.db",
  "gdrive_sync_path": "G:/My Drive/StdyTime/Stdytime.db",
  "sync_interval_minutes": 5,
  "startup_pull_from_gdrive": false
}

WHAT THESE SETTINGS MEAN
------------------------
- db_path:
  The main local database used while the app is running.

- gdrive_sync_path:
  The Google Drive backup copy.

- sync_interval_minutes:
  How often the local database is pushed to Google Drive.
  Example: 5 means every 5 minutes.

- startup_pull_from_gdrive:
  If false, the local database remains the source of truth on startup.
  Recommended setting: false

RECOMMENDED SETTINGS
--------------------
For most Windows users:
- Keep db_path on the local hard drive.
- Use Google Drive only for backup/sync.
- Keep startup_pull_from_gdrive set to false.

BEST PRACTICES
--------------
- Let Stdytime close normally so the final database sync can complete.
- Do not shut down Windows immediately after closing the app if a sync is in progress.
- If using multiple computers, make sure only one machine is actively using the shared Google Drive database backup workflow at a time.

TROUBLESHOOTING
---------------
If the app says the database path is not writable:
- Check that the folder exists.
- Check that you have permission to write to the folder.
- Check that Google Drive is signed in and syncing.

If backup does not appear in Google Drive:
- Recheck gdrive_sync_path in db_config.json.
- Make sure the Google Drive folder is available locally in File Explorer.
- Restart the app after saving db_config.json.

FILES YOU MAY NEED
------------------
- Stdytime.exe
- db_config.json.example
- INSTALL_README_WINDOWS.txt
- VERSION

SUPPORT NOTES
-------------
HTTPS is not required for local-only use on the Windows PC.
The app is designed to start on localhost unless you explicitly change its host settings.
