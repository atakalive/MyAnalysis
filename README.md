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

**Prerequisites**: Windows 11 / Python 3.11 or later / Git for Windows / the Claude Code extension installed in VS Code and signed in. To use another engine → [AI engines](#ai-engines)

> [!WARNING]
> The agent runs commands with your user privileges, without asking for confirmation. Your requests and the data it reads are sent to the engine's provider. → [Security](#security)

1. **Install** (in Command Prompt, in a local folder that is not a sync folder such as OneDrive)

   ```bat
   git clone https://github.com/atakalive/MyAnalysis.git
   cd MyAnalysis
   py -3 -m venv .venv
   .venv\Scripts\activate
   python -m pip install -r requirements.txt
   ```

2. **Launch**: double-click `run.bat`. The UI starts in English (to switch to Japanese: **Settings → Language / 言語 → 日本語**). If no window appears → [Troubleshooting](#troubleshooting)
3. **AI engine**: in **Settings → Backend / model settings…**, choose "Claude (VS Code bundled engine)" and press **Apply**. On the second and later PCs that use [R2 config sync](docs/config_sync_ja.md), first read [the section for the second and later PCs](docs/config_sync_ja.md#2-台目以降).
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

- Name the venv `.venv`. `run.bat` looks for this name only.
- Run commands that start with `python` from the repository root with the venv activated.
- To update, `git pull` and restart. Datasets are not affected.

- If `activate` fails in PowerShell, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` or use Command Prompt.
- To open TIFF images, also install `pip install tifffile`.
- Package versions are not pinned. If something does not work, match the tested versions (Python 3.12.1 / PySide6 6.10.1 / pyqtgraph 0.14.0 / numpy 1.26.4 / pandas 2.2.0 / matplotlib 3.8.2).
- To try the screens without data, run `python tool.py --demo`.
- On macOS / Linux: `python3 -m venv .venv` → `. .venv/bin/activate` → `pip install -r requirements.txt` → `python tool.py` (untested).

---

## AI engines

- Claude is recommended (it is the most tested).
- Only Claude Code / Codex CLI / pi can run analyses (execute Python). The OpenAI-compatible HTTP engine can only operate the GUI, such as opening tabs.
- The **model field** can be left empty; the engine's default model is used. To specify one: for Claude, an alias such as `opus` / `sonnet` or a name shown by `/model` in Claude Code; for Codex and pi, a catalog ID such as `gpt-5.5` (the dialog's suggestions, or `pi --list-models`); for OpenAI-compatible engines, the server-side model name (`ollama list` for Ollama). Values for thinking / effort: [docs/engines_ja.md](docs/engines_ja.md#モデルの指定).

To use an engine other than Claude (Codex CLI / pi / an OpenAI-compatible HTTP server such as Ollama), to change the model per chat, or to write the configuration file by hand, read [docs/engines_ja.md](docs/engines_ja.md).

---

## Datasets

A dataset is a pair of "one folder containing measurement data" and "its name". Registration is per PC.

- **Name**: names containing whitespace, `/`, `\`, or starting with `.` cannot be registered. **Avoid names that contain symbols (`"` `'` `&` `$`) or start with `-`.** They can be registered and opened from the GUI, but agent and CLI commands may treat them as options or break the quoting, so you cannot specify them there.
- **Data format**: any layout (the agent inspects it before reading). A layout where each subfolder has one CSV file with the same name can be read as-is by the standard loader.
- **What the app creates in the folder**: `myanalysis.toml` (settings), `meta.json` (for the list view), `analyses/` (analysis modules), `_work/` (outputs, chat history, tab layout). You need write permission on the folder. The app itself does not modify measurement files. The agent is instructed not to modify them, but this is not enforced mechanically.
- **Using a dataset on several PCs via a sync drive**: register it with the same name on each PC. **Do not open the same dataset on two PCs at the same time** (there is no locking between PCs; for the tab layout, and for a chat changed on both PCs, the later save wins). The first send on another PC resends the whole chat history.

To remove a registration, select the dataset in **File → Open dataset…** and press **Remove registration** (files in the folder are not deleted). To register from the CLI, or to change the output location or read format (`myanalysis.toml`), read [docs/usage_ja.md](docs/usage_ja.md#データセット).

---

## Usage

**The agent produces three kinds of output.**

| Kind | Kept? | Location |
|---|---|---|
| Code written and run on the spot | **Not kept**. If you want to look at it later, ask "save the code too" | — |
| Figures, code, and reports you had it save | Kept. The figures shown are usually files saved here | the dataset's `_work/` (`figures/`, `code/`, etc.) |
| Analysis modules (interactive analysis tabs) | Kept. There is no menu to open them; ask in the chat, e.g. "open `<analysis name>`" | the dataset's `analyses/<analysis name>/analysis.py` (→ [docs/analysis_module_ja.md](docs/analysis_module_ja.md)) |

- The agent uses the Python in `.venv` and may pip install missing packages (such as scipy) into `.venv`.
- Right-click a chat tab → **Close** **deletes** the chat. To keep it, use **Archive**.

To view microscope images and the like as in ImageJ (16-bit / multi-dimensional TIFF, LUTs, channel composites), use the image viewer. Ask in the chat, e.g. "open `<file path>` in the image viewer" (the agent opens the image viewer only when asked), or open it from the CLI. For the steps, read [docs/usage_ja.md](docs/usage_ja.md#図ビューアと画像ビューア).

---

## Saving and resuming

- **The tab layout and chats are not saved automatically.** Save them with **File → Save session** or **Save and quit**. After a crash or a forced exit, chats since the last save are lost.
- Closing with unsaved changes asks for confirmation. However, chats used without opening any dataset are not saved, and no confirmation is shown for them.
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
| No window appears with `run.bat` | A dialog shows the cause. If Python is too old, there is only the dialog (nothing is logged). Otherwise a traceback is left in `data/logs/gui-crash-*.log` (logs at warning level and above go to `data/logs/myanalysis.log`) |
| The first send fails with HTTP 401 | No AI engine is configured → step 3 of [QuickStart](#quickstart) |
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
