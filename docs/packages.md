# Packages

Packages patch the SyKit tool folder itself: they can add new files, edit
existing ones, and remove files. Every install is recorded as a reversible
diff, so removing a package restores SyKit to the exact state it would be in
had the package never been added.

> [!WARNING]
> A package can replace executable SyKit source. Installing one grants it the
> same trust as running its code. Review the complete package and only install
> packages from sources you trust.

## Commands

```
python SyKit package add <path/to/package>   Install a package folder
python SyKit package remove <id>             Uninstall as if it was never added
python SyKit package list                    Show installed packages in order
python SyKit package diff <id or *>          Show what a package changed
```

## Creating a package

1. Make a folder.
2. Add a `SyKitPackage.json`:

```json
{
    "id": "my-package",
    "name": "My Package",
    "desc": "What it does.",
    "package-req": ["some-other-id"],
    "credit": ["John Doe (https://example.com)"]
}
```

   - `id` **required**, letters, digits, `.`, `_`, `-` (must start with a
     letter or digit). IDs are matched without regard to case and may not use
     filesystem or SyKit metadata names such as `CON`, `index.json`, or
     `authors.md`. This is the name used by `remove` and `diff`.
   - `name`, `desc` **optional**, shown by `list`. Printed metadata may not
     contain terminal control characters.
   - `package-req` **optional**, package ids that must already be installed
     before this one can be added.
   - `credit` **optional**, a string or list of strings naming the package's
     authors. Values may not contain terminal control characters. While the
     package is installed, they are listed in `.packages/authors.md` (removed
     again when no credited package remains).

3. Create `add/`, `edit/` and/or `remove/` folders inside it (only the ones
   you need). A `README`/`LICENSE` and hidden files are ignored; anything
   else at the top level is an error.

All paths inside a package mirror SyKit's own layout, relative to the SyKit
folder. Packages may not touch `.git/`, `.packages/`, or `__pycache__/`,
including aliases that differ only by letter case.

### `add/` new files

Every file under `add/` is created at the same path inside SyKit:

```
add/files/test.txt  ->  SyKit/files/test.txt
```

Adding a file that already exists is an error (use `edit/` for that).
Missing folders are created automatically and cleaned up again on removal.

### `edit/` changing existing files

An edit is a payload file plus an optional instruction file named
`<file>.json` next to it:

```
edit/files/frontend/index.html        <- content to insert (the payload)
edit/files/frontend/index.html.json   <- how to apply it
```

Without the `.json`, the payload simply **replaces the whole target file**.

The `.json` holds one edit object, or a list of them applied in order:

```json
[
    { "action": "insert-after", "anchor": "<head>" },
    { "action": "append", "content": "<!-- footer -->\n" }
]
```

- `action`: one of:
  - `replace-file`: replace the entire file
  - `append` / `prepend`: add content at the end / start of the file
  - `insert-after` / `insert-before`: insert content right after / before
    the first occurrence of `anchor`
  - `replace`: replace the first occurrence of `anchor` with the content
- `anchor`: literal text to search for (required for `insert-*` and
  `replace`; it is an error if the anchor is not found).
- `content`: optional inline text; when omitted, the payload file's
  content is used. Content is inserted verbatim (bring your own newlines).

Note: a file named `X.json` sitting next to a file named `X` inside `edit/`
is always treated as the instruction file for `X`, never as a payload.
Editing a file that does not exist in SyKit is an error.

### `remove/` deleting files

`remove/` contains one or more `.json` files, each a plain list of SyKit
paths to delete:

```json
["files/core/endpoints.mjs", "files/frontend/old.css"]
```

Only files can be listed (not folders), and they must exist.

## Viewing / undoing changes

When a package is added, its diff and its position in the install order are
stored as an entry in SyKit's `.packages/` folder (a copy of the package
source, before/after snapshots of every touched file, and a `record.json`).

- `python SyKit package diff <id>` prints the recorded unified diff;
  `diff *` prints every installed package in order.
- `python SyKit package remove <id>` reverses the diff. If the package is, say,
  3rd of 5, packages 5 and 4 are unwound first, then 3 is reversed, then 4
  and 5 are re-applied from their stored copies, so it is as if 3 was
  never there.
- Removal fails up front if a remaining package lists the id in its
  `package-req`. If re-applying a later package fails for any other reason
  (for example its anchor text came from the removed package), the whole
  removal is rolled back and nothing changes.

A package cannot be added twice; remove it first, then add the new version.
