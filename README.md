# MyAnalysis

[English](README.md) | [日本語](README_ja.md)

A desktop app (Python / PySide6) for analyzing measurement data by chatting with an AI agent.

Ask "plot the relationship between A and B", and the agent inspects how the data is organized, analyzes and plots it in Python, and shows the figure in a tab. The chat history and the analysis code, figures, and reports the agent saved are kept in the dataset's folder, so if you put the folder on a sync drive you can pick up where you left off on another PC.

**Repositories:**
- **GitHub (stable):** <https://github.com/atakalive/MyAnalysis>
- **GitLab (development):** <https://gitlab.com/atakalive/MyAnalysis>

The detailed documents under `docs/` are available in Japanese only.

---

## QuickStart

**Prerequisites**: Windows 11 / Python 3.11 or later (tested with 3.12; the python.org build) / Git for Windows (for clone and update, and for Claude Code's Bash tool) / the Claude Code extension installed in VS Code and signed in. To use another engine → [AI engines](#ai-engines)

> [!WARNING]
> The agent runs commands with your user privileges, without asking for confirmation. Your requests and the data it reads are sent to the engine's provider. → [Security](#security)

1. **Install**: run in Command Prompt (Win+R → `cmd`). Put it somewhere that is not a sync folder such as OneDrive.

   ```bat
   cd /d %USERPROFILE%
   git clone https://github.com/atakalive/MyAnalysis.git
   cd MyAnalysis
   py -3 -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

   If `py` is missing (Microsoft Store Python), use `python -m venv .venv` instead.

2. **Launch**: double-click `run.bat` in the `MyAnalysis` folder (no need to activate the venv). The UI starts in English (to switch to Japanese: **Settings → Language / 言語 → 日本語**). If no window appears → [Troubleshooting](#troubleshooting)
3. **AI engine**: open **Settings → Backend / model settings…** and choose **Claude (VS Code bundled engine)** under "Engine (how to run)". The model field can be left empty. Press **Connectivity check**; when it shows `OK (…s / <model name>)`, press **Apply**. If it shows "Failed", check that you are signed in to the Claude Code extension in VS Code. On the second and later PCs that use [R2 config sync](docs/config_sync_ja.md), first read [the section for the second and later PCs](docs/config_sync_ja.md#2-台目以降).
4. **Dataset**: choose the folder with your measurement data in **File → New dataset**. The folder name is filled in as the name; change it if it contains symbols or starts with `-` (→ [Datasets](#datasets)). When registration finishes, the dataset opens.
5. **Ask**: type something like "describe what is in this dataset" or "plot the relationship between `<column A>` and `<column B>`" in the chat pane on the right and press **Ctrl+Enter**. A reply can take from tens of seconds to a few minutes, and figures open in tabs on the left. Code written on the spot is not kept, so if you want to reproduce it, ask "save the code too" (→ [Usage](#usage)). If you see `[error: …]` (`[エラー: …]` in the Japanese UI) → [Troubleshooting](#troubleshooting)
6. **Save**: the tab layout and chats are not saved automatically. Use **File → Save session** often, and **File → Save and quit** when you are done (→ [Saving and resuming](#saving-and-resuming)).

---

## Contents

- [Installation and launch](#installation-and-launch)
- [AI engines](#ai-engines)
- [Datasets](#datasets)
- [Usage](#usage)
- [Saving and resuming](#saving-and-resuming)
- [Security](#security)
- [Optional features](#optional-features)
- [Troubleshooting](#troubleshooting)
- [Uninstall](#uninstall)
- [Limitations](#limitations)
- [For developers](#for-developers)
- [License](#license)

---

## Installation and launch

- Name the venv `.venv`. `run.bat` looks for this name only and activates it itself.
- Commands that start with `python` (the CLI, etc.) are run in the `MyAnalysis` folder after `.venv\Scripts\activate`, every time you open a new Command Prompt.
- To update, close the app, run `git pull` and then `.venv\Scripts\python -m pip install -r requirements.txt`, and launch again. Datasets are not affected.

- If `activate` fails in PowerShell, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` or use Command Prompt.
- To open TIFF images, also run `.venv\Scripts\python -m pip install tifffile`.
- Package versions are not pinned. If something does not work, match the tested versions (Python 3.12.1 / PySide6 6.10.1 / pyqtgraph 0.14.0 / numpy 1.26.4 / pandas 2.2.0 / matplotlib 3.8.2 / Pillow 10.3.0. These are the versions for Python 3.12).
- To try the screens without data, run `python tool.py --demo`.
- On macOS / Linux: `python3 -m venv .venv` → `.venv/bin/python -m pip install -r requirements.txt` → `.venv/bin/python tool.py` (untested).

---

## AI engines

Set it up in step 3 of [QuickStart](#quickstart).

- Claude is recommended (it is the most tested). "Claude (VS Code bundled engine)" uses the `claude` command on PATH when the VS Code extension is not found.
- Claude Code, Codex CLI, and pi are meant to be used by logging in to each engine (a subscription to the respective service, or a local llama.cpp server through pi). Pay-per-use API keys are not the intended path and are untested; the app has no API-key setting (only the OpenAI-compatible HTTP engine reads `OPENAI_API_KEY` from `.env`).
- Only Claude Code / Codex CLI / pi can run analyses (execute Python). The OpenAI-compatible HTTP engine can only operate the GUI, such as opening tabs.
- **What to put in the model field**
  - Claude / Codex: can be left empty (that engine's default model). To specify one for Claude, use an alias such as `opus` / `sonnet` / `haiku`. For Claude, the result of **Connectivity check**, `OK (…s / <model name>)`, shows the official model ID, which can be used as is. Both Claude and Codex have suggestions in the dialog, and **Fetch models** in **Settings → Backend status…** adds the newer IDs the engine knows to the suggestions.
  - pi: choose the **Provider** (`openai-codex` / `github-copilot` / `llama.cpp`); the model is an ID shown by `pi --list-models` (the dialog has suggestions; **Fetch models** adds more).
  - OpenAI-compatible HTTP: the server-side model name (the names shown by `ollama list` for Ollama). When empty, `OPENAI_MODEL` in `.env` is used, and if that is also unset, `gpt-4o-mini`, which fails with Ollama and the like.
- Effort (how much it thinks) can be chosen in **Settings → Backend / model settings…** (Claude / Codex / pi; also per chat). Usually leave it at "Default" (→ [docs/engines_ja.md](docs/engines_ja.md#モデルの指定)).

To use an engine other than Claude (Codex CLI / pi / an OpenAI-compatible HTTP server such as Ollama), to change the model per chat, or to write the configuration file by hand, read [docs/engines_ja.md](docs/engines_ja.md).

---

## Datasets

A dataset is a pair of "one folder containing measurement data" and "its name". Registration is per PC.

- **Name**: names containing whitespace, `/`, `\`, or starting with `.` cannot be registered. **Avoid names that contain symbols (`"` `'` `&` `$`) or start with `-`.** They can be registered and opened from the GUI, but agent and CLI commands may treat them as options or break the quoting, so you cannot specify them there.
- **Data format**: any layout (the agent inspects it before reading). A layout where every subfolder contains one CSV file with the same file name (e.g. `data.csv`) can be read as-is by the standard loader.
- **What the app creates in the folder**: `myanalysis.toml` (settings), `meta.json` (for the list view), `analyses/` (analysis modules), `_work/` (outputs, chat history, tab layout). You need write permission on the folder. The app itself does not modify measurement files. The agent is instructed not to modify them, but this is not enforced mechanically.
- **Using a dataset on several PCs via a sync drive**: register it with the same name on each PC. **Do not open the same dataset on two PCs at the same time** (there is no locking between PCs; for the tab layout, and for a chat changed on both PCs, the later save wins). The first send on another PC resends the whole chat history.

To remove a registration, select the dataset in **File → Open dataset…** and press **Remove registration** (this removes it from this PC's registry together with the paths for every host; with R2 sync, the registration on the other PCs is removed too; files in the folder are not deleted). A row shown as "No path on this host" (registered on another PC and synced here) becomes openable once you press **Register path on this PC…** in the same list and select the folder on this PC. To register from the CLI, or to change the output location or read format (`myanalysis.toml`), read [docs/usage_ja.md](docs/usage_ja.md#データセット).

---

## Usage

**Everything the agent produces (figures, code, reports) is saved under the dataset's `_work/` (`figures/`, `code/`, etc.).** The figures shown are usually files saved there.

- The agent uses the Python in `.venv` and may pip install missing packages (such as scipy) into `.venv`.
- Right-click a chat tab → **Close** **deletes** the chat. To keep it, use **Archive**.
- **Ctrl+←/→** switches to the previous/next analysis tab and **Alt+←/→** to the previous/next dataset, wherever the focus is (in a text input, Ctrl+←/→ moves the cursor by word instead).
- **Ctrl+F** (**View → Search chats…**) searches past chats and lists the hits in a new chat tab. With **AI search** on, the agent looks through your chats, sums up what was concluded, and answers follow-up questions (→ [docs/usage_ja.md](docs/usage_ja.md#チャット)).

To view microscope images and the like as in ImageJ (16-bit / multi-dimensional TIFF, LUTs, channel composites), use the image viewer. Ask in the chat, e.g. "open `<file path>` in the image viewer" (the agent opens the image viewer only when asked), or open it from the CLI. For the steps, read [docs/usage_ja.md](docs/usage_ja.md#図ビューアと画像ビューア).

---

## Saving and resuming

- **The tab layout and chats are not saved automatically.** Save them with **File → Save session** or **Save and quit**. After a crash or a forced exit, chats since the last save are lost.
- Closing with unsaved changes asks for confirmation. However, a chat that never became attached to a dataset is not saved, and no confirmation is shown for it (when a chat gets attached: [docs/usage_ja.md](docs/usage_ja.md#チャット)).
- To resume, use **File → Open dataset…** (restores that dataset's tabs and chats) or **File → Restore last session** (opens all datasets that were open at the last save).

What is saved in which file: [docs/usage_ja.md](docs/usage_ja.md#保存データと再開).

---

## Security

- **The agent runs with your user privileges, without asking for confirmation.** By default Claude runs with `bypassPermissions`, Codex without a sandbox, and pi with all tools allowed.
- **Your requests and the data the agent reads are sent to the engine's provider.** The agent also communicates externally through the shell, web search, and so on. MyAnalysis itself sends no telemetry.
- The agent can read `.env` and shell environment variables (API keys, etc.).

To restrict the agent's permissions, read [docs/security_ja.md](docs/security_ja.md) (restricting them breaks GUI operations and analyses).

---

## Optional features

| Feature | Description | Document |
|---|---|---|
| Your own analysis modules | Build an interactive analysis tab from a single Python file. Export PNGs in batch without the GUI | [docs/analysis_module_ja.md](docs/analysis_module_ja.md) |
| CLI | Operate the GUI, register datasets, and run checks from the command line | [docs/cli_ja.md](docs/cli_ja.md) |
| Config sync across PCs | Sync the registry and engine settings between PCs via Cloudflare R2 | [docs/config_sync_ja.md](docs/config_sync_ja.md) |
| Meeting share | Share the analysis view and chat to a remote participant's browser (connecting to outside participants requires Cloudflare setup). **Anyone with the join link can run the agent without approval** | [docs/meeting_share_ja.md](docs/meeting_share_ja.md) |

---

## Troubleshooting

| Symptom | What to do |
|---|---|
| No window appears with `run.bat` | A dialog shows the cause. If Python is too old, there is only the dialog (nothing is logged). Otherwise a traceback is left in `data/logs/gui-crash-*.log` (logs at warning level and above go to `data/logs/myanalysis.log`). If it says "A required package is missing", run `.venv\Scripts\python -m pip install -r requirements.txt` in the `MyAnalysis` folder (if `.venv` does not exist, redo step 1 of QuickStart). A Windows "pythonw not found" error also means `.venv` is missing. To see details, activate the venv and run `python tool.py` |
| `'py' is not recognized as an internal or external command`, or `py -3` fails with `Unable to create process` | Python was installed from the Microsoft Store or without the launcher, or the launcher points to a Python that was removed (`py -0p` shows the paths). Install Python from python.org (the launcher is included by default) and redo step 1. If you keep the Microsoft Store build, `python -m venv .venv` works instead (the note under step 1) |
| The chat says "No AI engine is selected, so nothing can be sent" | No AI engine is configured → step 3 of [QuickStart](#quickstart) |
| `[error: RuntimeError('Claude Code engine not found…')]` | Install the Claude Code extension and sign in (or point to `claude` with `[claude_code].bin` in `llm_backend/config.toml` or the `CLAUDE_CODE_BIN` environment variable) |
| The chat shows `[error: …]` | Expired authentication, rate limits, etc. Send again, or log in to the engine again |
| "Save failed verification" | The sync drive is misbehaving. Common with mounts such as rclone → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md) (includes the check-and-repair command `doctor`) |

Other symptoms, problems with sync drives, and what to include when reporting a bug → [docs/troubleshooting_ja.md](docs/troubleshooting_ja.md)

---

## Uninstall

1. Delete the app folder (the repository) and `%USERPROFILE%\.myanalysis` (`~/.myanalysis` on macOS / Linux).
2. To restore a dataset folder, delete `myanalysis.toml`, `meta.json*`, `analyses/`, and `_work/`. `analyses/` holds the analysis modules and `_work/` holds the chat history and outputs.

To also remove the engines' conversation records and the R2 / Cloudflare settings, read [docs/usage_ja.md](docs/usage_ja.md#アンインストールの詳細).

---

## Limitations

- Tested on Windows 11 only.
- An analysis tab keeps its data in memory while it is open. Opening many large datasets puts pressure on memory.
- In the Japanese UI, some displays remain in English (→ [display language](docs/usage_ja.md#表示言語)).
- Details → [docs/usage_ja.md](docs/usage_ja.md#制限事項の詳細)

---

## For developers

For developing MyAnalysis itself (structure, tests, hot reload, conventions), see [docs/development_ja.md](docs/development_ja.md). Conventions for AI coding agents are in [CLAUDE.md](CLAUDE.md).

---

## License

MIT License ([LICENSE](LICENSE))
