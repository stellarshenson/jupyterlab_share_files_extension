# Share Files - UX Design Scenarios

## The Two Things You Can Do

| Action               | What it means                                     | Who uses the link                 |
| -------------------- | ------------------------------------------------- | --------------------------------- |
| **Create a Share**   | "Here are my files/folders, grab them" (readonly) | Anyone with the link can download |
| **Create a Request** | "Send me files/folders here"                      | Anyone with the link can upload   |

Both produce a link. You paste it in a chat. Done.

## Terminology

- **Share** = named readonly file/folder drop you created. Others download from it
- **Request** = named inbox you created. Others upload files/folders to it
- **Connection** = you pasted someone else's link into your panel. You're connected to their share or request

You can share individual files, entire folders, or a mix of both. Folders are copied recursively - the directory structure is preserved.

---

## Local Storage

Everything lives under `.jupyterlab_shares/` in the workspace root (notebook server root). This folder is invisible in the file browser (dot-prefixed).

```
.jupyterlab_shares/
  shares/
    training-data/          # one folder per share you created
      manifest.json         # name, id, created_at, entries[]
      data/
        train.csv           # copied files
        test.csv
    project-export/
      manifest.json
      data/
        src/                # copied folders preserve structure
          model.py
          utils/
            helpers.py
        README.md
  requests/
    homework-week-5/        # one folder per request you created
      manifest.json         # name, id, created_at
      uploads/
        bob/                # subfolder per uploader
          solution.py
        charlie/
          solution.py
          results/          # uploaded folders keep structure too
            output.csv
  connections.json          # list of links you've connected to
```

Files and folders in shares are **copies** - originals stay untouched. Folder structure is preserved recursively. The share is a frozen snapshot - it won't change if you edit the originals.

---

## Scenario 1: Alice shares files and a folder with Bob

**Alice's side:**

1. Alice selects `train.csv`, `test.csv` and the `src/` folder in file browser → right-click → **"Share Files..."**
2. Dialog: `Name: [training data___]  [Create]`
3. Files and folders copied recursively to `.jupyterlab_shares/shares/training-data/data/`
4. Link appears in dialog with [Copy Link] button. Alice copies it.

```
 Alice's Panel (after)
+-------------------------------+
| Share Files              [+]  |
+-------------------------------+
| v MY SHARES (1)               |
|   training data     4 items   |
|   > train.csv            [x]  |
|   > test.csv             [x]  |
|   v src/                 [x]  |  <-- folder, expandable
|     > model.py                |
|     > utils/                  |
+-------------------------------+
```

The [x] on a folder removes the entire folder from the share.

5. Alice pastes link in Slack.

**Bob clicks the link in a browser (standalone page):**

```
+--------------------------------------------------+
|                                                    |
|   training data                                    |
|   shared by alice              4 items, 3.2 MB     |
|                                                    |
|   +----------------------------------------------+ |
|   | train.csv          1.2 MB     [Download]     | |
|   | test.csv           800 KB     [Download]     | |
|   | v src/                        [Download ZIP] | |
|   |     model.py       400 KB                    | |
|   |     v utils/                                 | |
|   |         helpers.py  50 KB                    | |
|   +----------------------------------------------+ |
|                                                    |
|   [Download All as ZIP]                            |
|                                                    |
+--------------------------------------------------+
```

Folders can be downloaded individually as ZIP, or everything via "Download All as ZIP."

**Bob connects via his JupyterLab panel instead:**

```
 Bob's Panel
+-------------------------------+
| Share Files              [+]  |
+-------------------------------+
| v CONNECTED (1)               |
|   training data      (alice)  |
|   > train.csv       1.2 MB   |
|   > test.csv        800 KB   |
|   v src/                      |
|     > model.py      400 KB   |
|     > utils/                  |
|   [Save All]    [Disconnect]  |
+-------------------------------+
| [Paste link...____________]   |
+-------------------------------+
```

- **Save All** → creates a folder named after the share (e.g. `training-data/`) in Bob's current file browser directory, with all contents inside preserving structure (spinner). If `training-data/` already exists, appends a number (`training-data-2/`)
- **Disconnect** → removes this connection from Bob's panel
- Clicking a file row → saves just that file directly into the current directory (spinner on row)
- Clicking a folder row → saves that folder (by name) into the current directory (spinner on row)

```
 Bob's file browser after "Save All":
+-----------------------------+
| > my-notebook.ipynb         |
| > notes.txt                 |
| v training-data/       <--- new folder, named after the share
|   > train.csv               |
|   > test.csv                |
|   v src/                    |
|     > model.py              |
|     > utils/                |
+-----------------------------+
```

---

## Scenario 2: Alice creates a file request, Bob uploads

1. Alice clicks [+] → "New Request"
2. Dialog: `Name: [homework week 5___]  [Create]`
3. Link appears with [Copy Link]. Alice posts it in class chat.

```
 Alice's Panel
+-----------------------------+
| Share Files            [+]  |
+-----------------------------+
| v MY REQUESTS (1)           |
|   homework week 5           |
|     0 uploads               |
+-----------------------------+
```

**Bob opens the request link (standalone page):**

```
+--------------------------------------------------+
|                                                    |
|   homework week 5                                  |
|   requested by alice                               |
|                                                    |
|   +----------------------------------------------+ |
|   |                                              | |
|   |     Drop files here to upload                | |
|   |                                              | |
|   |         or [Browse files...]                 | |
|   |                                              | |
|   +----------------------------------------------+ |
|                                                    |
|   Uploading...  [=========>        ] 65%           |  <-- spinner/progress
|                                                    |
+--------------------------------------------------+
```

**Bob connects via JupyterLab instead:**

```
 Bob's Panel
+-----------------------------+
| Share Files            [+]  |
+-----------------------------+
| v CONNECTED (1)             |
|   homework week 5  (alice)  |
|   [ Drop files here ]      |  <-- drag target
|   [Disconnect]              |
+-----------------------------+
```

Bob drags files from file browser onto the connected request row → files upload → spinner on the row during upload.

**Alice gets a notification** (JupyterLab notification API, not toast):

```
+--------------------------------------+
| bob uploaded 1 file to               |
| "homework week 5"                    |
+--------------------------------------+
```

**Alice's panel updates:**

```
| v MY REQUESTS (1)           |
|   homework week 5           |
|     1 upload                |
|   v bob/                    |
|     > solution.py      [x]  |  <-- [x] removes this upload
|   [Save All]                |
+-----------------------------+
```

Alice clicks **Save All** → creates a folder named after the request (e.g. `homework-week-5/`) in her current file browser directory. Uploads are organized by uploader inside:

```
 Alice's file browser after "Save All":
+-----------------------------+
| v homework-week-5/     <--- new folder, named after the request
|   v bob/                    |
|     > solution.py           |
|   v charlie/                |
|     > solution.py           |
|     v results/              |
|       > output.csv          |
+-----------------------------+
```

---

## Scenario 3: Non-JupyterLab users

Same standalone pages. No panel. Just the link in a browser. Works for shares (download) and requests (upload).

---

## All Actions

### On a Share you own

| Action       | Where                                               | What happens                              |
| ------------ | --------------------------------------------------- | ----------------------------------------- |
| Copy Link    | Context menu on share row                           | Link to clipboard                         |
| Add Items    | Drag files/folders from file browser onto share row | Items copied into the share (spinner)     |
| Remove Item  | [x] button on a file or folder row                  | Item removed (folder removed recursively) |
| Delete Share | Context menu on share row                           | Entire share deleted after confirm        |

### On a Request you own

| Action         | Where                       | What happens                                                                                         |
| -------------- | --------------------------- | ---------------------------------------------------------------------------------------------------- |
| Copy Link      | Context menu on request row | Link to clipboard                                                                                    |
| Save All       | Button on request           | Creates `<request-name>/` folder in current dir with uploads inside, organized by uploader (spinner) |
| Remove Upload  | [x] on an uploaded file     | That file removed                                                                                    |
| Delete Request | Context menu on request row | Entire request deleted after confirm                                                                 |

### On a Connection (someone else's share or request)

| Action       | Where                                                       | What happens                                                                         |
| ------------ | ----------------------------------------------------------- | ------------------------------------------------------------------------------------ |
| Save All     | Button on connected share                                   | Creates `<share-name>/` folder in current dir with all contents inside (spinner)     |
| Save Item    | Click a file or folder row                                  | File saves directly to current dir; folder saves as named subfolder (spinner on row) |
| Upload Items | Drag files/folders from file browser onto connected request | Items uploaded (spinner)                                                             |
| Disconnect   | Button below connection                                     | Connection removed from panel                                                        |

---

## Drag-and-Drop Details

### Dragging INTO the panel (file browser → panel)

This uses Lumino's drag events (`lm-dragenter`, `lm-dragover`, `lm-drop`). The file browser already sets the MIME data when you start dragging. The panel listens for drops.

| Drop target                | Visual feedback      | Result                            |
| -------------------------- | -------------------- | --------------------------------- |
| Drop zone (empty area)     | Zone highlights blue | "Create new share" dialog         |
| Existing share row (yours) | Row highlights blue  | Files/folders added to share      |
| Connected request row      | Row highlights blue  | Files/folders uploaded to request |
| Anything else              | No highlight         | Drop rejected                     |

Folders are copied/uploaded recursively - structure preserved. During the operation, the target row shows a **spinner** replacing the item count until the copy/upload completes.

### Dragging FROM the panel (panel → file browser)

**Not supported.** Files and folders flow out of the panel via **click-to-save** instead:

- Click a file row in a connected share → file saves to current file browser directory
- Click a folder row → folder saves recursively to current directory
- Click "Save All" → everything saves, preserving structure

Why not drag: connected share items live on a remote server. Dragging would require downloading first, then faking a Lumino drag event - same result as click-to-save but fragile and with no visual benefit. The spinner on the row during download gives clearer feedback than a drag that might hang mid-flight.

---

## Panel Layout (complete)

```
+-------------------------------+
| [icon] Share Files   [+]  [R] |  [+] = new share or request
+-------------------------------+  [R] = refresh
| v MY SHARES (2)          [v]  |  [v] = collapse section
|                               |
|   training data     4 items   |
|   > train.csv           [x]  |
|   > test.csv            [x]  |
|   v src/                [x]  |  <-- folder, collapsible
|     > model.py               |
|     > utils/                 |
|                               |
|   screenshots       1 item   |
|   > screen1.png         [x]  |
|                               |
+-------------------------------+
| v MY REQUESTS (1)        [v]  |
|                               |
|   homework week 5             |
|     2 uploads                 |
|   v bob/                      |
|     > solution.py        [x]  |
|   v charlie/                  |
|     > solution.py        [x]  |
|     v results/           [x]  |
|       > output.csv            |
|   [Save All]                  |
|                               |
+-------------------------------+
| v CONNECTED (1)          [v]  |
|                               |
|   training data    (alice)    |
|   > train.csv      1.2 MB    |
|   > test.csv       800 KB    |
|   v src/                      |
|     > model.py     400 KB    |
|     > utils/                 |
|   [Save All]   [Disconnect]   |
|                               |
+-------------------------------+
|                               |
|  +---------------------------+|
|  | Drag files here to share  ||  <-- drop zone
|  | or paste a link below     ||
|  +---------------------------+|
|                               |
|  [Paste link...____________] |  <-- connect field
|  [Connect]                    |
+-------------------------------+
```

Folders show as collapsible tree nodes with `v` (expanded) or `>` (collapsed). Only the first level is expanded by default - deeper nesting is collapsed until clicked.

### Right-click context menus on rows

**On a share row:**

```
  Copy Link
  ─────────
  Delete Share
```

**On a request row:**

```
  Copy Link
  ─────────
  Delete Request
```

**On a connected item:**

```
  Open in Browser    (opens standalone page)
  ─────────
  Disconnect
```

---

## Spinners

| Operation                      | Where spinner shows                    | Duration                   |
| ------------------------------ | -------------------------------------- | -------------------------- |
| Creating share (copying files) | On the new share row                   | Until copy completes       |
| Adding files to share          | On the share row, replacing file count | Until copy completes       |
| Saving file from connection    | On the file row being saved            | Until download completes   |
| Save All                       | On the Save All button                 | Until all files downloaded |
| Uploading to request           | On the connected request row           | Until upload completes     |
| Connecting to link             | On the Connect button                  | Until metadata fetched     |
| Deleting share/request         | On the row being deleted               | Until deletion completes   |

---

## Notifications

Only one type of notification: **incoming uploads to your requests**.

When someone uploads a file to a request you own, and you have JupyterLab open:

```
+--------------------------------------+
| [user] uploaded [N] file(s) to       |
| "[request name]"                     |
+--------------------------------------+
```

This uses JupyterLab's built-in notification API. Detected via polling (not WebSocket).

No other notifications. No toasts. The panel state is the source of truth.

---

## Key Constraint: Creator's Server Must Be Running

Files are served from the creator's server. No shared filesystem.

| Creator's server | What happens                                                  |
| ---------------- | ------------------------------------------------------------- |
| Running          | Links work, connections work                                  |
| Stopped          | Standalone page: "Share unavailable, owner is offline"        |
|                  | Panel connection: row shows "offline" badge, actions disabled |

---

## What This Is NOT

- Not a file sync service (shares are frozen snapshots)
- Not a virtual drive (no mounted folders)
- Not a collaboration tool (no editing)
- Not permanent storage (creator can delete anytime)

It's a link. You click it. You get files. Or you send files. That's it.
