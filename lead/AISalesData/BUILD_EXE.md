# Building AISalesMachine.exe

Written for: whoever builds the Windows package (needs a Windows machine).

## Why this can't be built here

PyInstaller bundles the **host platform's** Python runtime into the output.
It is not a cross-compiler, so a Windows `.exe` can only be produced on
Windows. There is no Linux or macOS workaround that yields a real `.exe`.

You need any Windows 10/11 64-bit machine — your own, a colleague's, or a
cloud VM for an hour.

## Build it

1. Copy this whole `AISalesData` folder to the Windows machine.
   **Leave out `.streamlit\secrets.toml`, `.sender_key`, `.venv`, `*.db`** —
   those are yours and must not ship.
2. Install **Python 3.12** from python.org, ticking *"Add Python to PATH"*.
3. Double-click **`build_windows.bat`**.

It creates a build venv, installs the pinned requirements plus PyInstaller,
and packages everything. First run takes 10–20 minutes.

Output: **`dist\AISalesMachine\`**

## Shipping it

Zip the **entire `AISalesMachine` folder**, not just the `.exe`. It is a
one-folder build — the executable needs the DLLs and data files beside it.
Expect roughly 500–700 MB zipped.

A one-file build was deliberately not used: it re-extracts the whole bundle
to a temp directory on every launch, which is slow and breaks the Chrome
subprocess paths the scraper depends on.

## What the recipient does

1. Unzip anywhere (Desktop is fine).
2. Run `AISalesMachine.exe`. A console window opens and the browser follows.
3. **First launch asks them to create a password.** It is saved to
   `.streamlit\secrets.toml` beside the exe, along with a freshly generated
   encryption key unique to their installation.
4. They add their own SMTP accounts under ⚙️ Manage Senders.

Their data — database, secrets, logs — lives **next to the executable**, so
it persists between runs and never touches your copy.

Keep the console window open; closing it stops the app.

## What they need for full functionality

| Feature | Requirement |
|---|---|
| List Campaign, Replies, follow-ups | nothing extra |
| AI-written emails | their own OpenAI key, pasted in the sidebar |
| **Scraper Campaign** | **Google Chrome installed** |

Chrome is not bundled — it is ~150 MB and licensing redistribution is a
question you do not want. Selenium finds an existing Chrome install and
downloads the matching driver itself on first use.

## Do not ship your secrets

`build_windows.bat` does not bundle `secrets.toml`, but if you copy your
working folder to the build machine, delete these first:

```
.streamlit\secrets.toml     your OpenAI key + encryption key
.sender_key                 fallback encryption key
sales_campaign.db           your leads, sent mail, saved SMTP passwords
```

Shipping `sales_campaign.db` would hand over your contact list **and** your
saved SMTP passwords — they are encrypted, but the key sits in the same
folder.

## If the build fails

**`PackageNotFoundError` at launch** — a dependency reads its own version at
runtime. Add it to the `copy_metadata` loop in `AISalesMachine.spec`.

**`ModuleNotFoundError` at launch** — add the module to `hiddenimports` in
the spec.

**Antivirus flags the exe** — common for unsigned PyInstaller output. Code
signing is the real fix; otherwise the recipient allows it explicitly.

**Blank browser page** — read the console window; Streamlit errors print