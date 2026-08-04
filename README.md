# LucyLive — AI Render for Cinema 4D

Real-time AI preview for the Cinema 4D viewport.

Created by [**VantageInc**](https://vantageinc.co/) / [**@thesibilev**](https://www.instagram.com/thesibilev/).

![Version](https://img.shields.io/badge/version-1.0.3-7c3aed)

## Download

- [Windows 10/11](https://github.com/VantageInc/LucyLive-AI-Render/releases/download/v1.0.3/LucyLive-v1.0.3-preview-windows.zip)
- [Apple Silicon Mac — experimental](https://github.com/VantageInc/LucyLive-AI-Render/releases/download/v1.0.3/LucyLive-v1.0.3-preview-macos-apple-silicon.zip)

Use these release ZIPs for installation. GitHub source archives do not include dependency wheels.

## Install

1. Download the ZIP for your platform and extract it.
2. Keep the complete `LucyLive` folder inside a folder used for Cinema 4D plugins.
3. In Cinema 4D, open **Edit → Preferences → Plugins → Add Folder** and select the parent plugins folder.
4. Restart Cinema 4D and open **Extensions → AI Render**.
5. Open **Settings…**, click **Install deps**, and wait for `Done`.
6. Restart Cinema 4D, add your fal.ai API key in **Settings…**, enter a prompt, and click **Start**.
7. Click **Stop** when finished.

**Install deps** uses Cinema 4D's bundled Python; no separate `c4dpy`, Command Line, or RLM license is required.

## Requirements

- Cinema 4D 2024–2026 with Python 3.11
- 64-bit Windows 10/11, or Apple Silicon with macOS 14+
- Native Apple Silicon mode; Rosetta and Intel Macs are not supported
- Internet access, a compatible fal.ai API key, model access and account balance

This is preview version **1.0.3**. The Mac build is experimental. Prompts, viewport frames, and optional reference images are processed by external cloud services and may incur usage charges.

Saved API keys are stored locally as plain text. Prefer the `FAL_KEY` environment variable on shared computers.

LucyLive is proprietary source-available, not open source. See the [LucyLive Preview License](LICENSE.md).
