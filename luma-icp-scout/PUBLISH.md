# Publishing to the Chrome Web Store (Unlisted) + updating continuously

## One-time setup
1. Go to the **Chrome Web Store Developer Dashboard**: https://chrome.google.com/webstore/devconsole
2. Sign in with the Google account you want to own the extension.
3. Pay the **one-time $5** registration fee (first time only).
4. Complete the account: verify email, set a publisher display name.

## Build the upload package
From the `luma-icp-scout/` folder:
```bash
./build.sh
```
This creates `../luma-icp-scout-v<version>.zip` with only the files the extension needs
(manifest at the zip root — required by Chrome).

## Submit as Unlisted
1. Dashboard → **Add new item** → upload the zip.
2. Fill the **Store listing** tab (copy from `store-listing.md`):
   - Name, summary, description
   - **At least 1 screenshot** (1280×800 or 640×400) — a shot of the side panel works
   - An icon is already in the package (128px)
   - Category: Productivity; language; etc.
3. **Privacy tab** (required):
   - Single purpose: "Find ICP-matching prospects from a Luma event's guest list."
   - Justify each permission (text in `store-listing.md`).
   - Data usage: declare that data stays local / only syncs to the user's own backend.
   - Add a privacy policy URL (a simple page is fine; can host on your site).
4. **Visibility** → set to **Unlisted**  ← this is the key step (not searchable; only people
   with the link can install; still one-click + auto-updating).
5. **Submit for review.** Review typically takes hours to a few days.

## ⚠️ Approval is not guaranteed
Reviewers scrutinize extensions that read/automate other sites (LinkedIn) and broad host
permissions. To improve odds:
- The LinkedIn *connect/automation* code has been removed (good).
- Consider narrowing `https://*/*` → `optional_host_permissions` (ask Claude to do this).
- Keep the description honest: it reads pages the signed-in user can already see, for their own use.
If Chrome rejects it, the **Edge Add-ons store** (free, more lenient) and **unpacked** are fallbacks.

## Updating continuously (after it's live)
Every time you want to ship changes:
1. Make the code changes.
2. Bump the version in `manifest.json`, e.g. `"version": "0.1.1"` → `"0.1.2"`
   (must be higher than the live version; use x.y.z numbers).
3. Run `./build.sh` to make the new zip.
4. Dashboard → your item → **Package → Upload new package** → upload the new zip.
5. Submit for review. Once approved, Chrome **auto-pushes the update** to all installs within
   a few hours — users don't do anything.

So the ongoing loop is just: edit → bump version → `./build.sh` → upload → submit. That's it.
