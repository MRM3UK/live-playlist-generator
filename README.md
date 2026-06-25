# 🔴 Live Cam M3U Playlist Generator

Automatically fetches **verified** live HLS stream links and generates an M3U playlist — updated every 10 minutes via GitHub Actions.

> ✅ Every stream URL is **validated** before being added to the playlist.  
> ✅ No fake links. No dead streams. No mismatched models.

---

## 📋 Table of Contents

- [Quick Start](#-quick-start)
- [How It Works](#-how-it-works)
- [Managing Favourites](#-managing-favourites)
- [Playlist URL](#-playlist-url)
- [Playlist Format](#-playlist-format)
- [Smart Logic](#-smart-logic)
- [Stream Verification](#-stream-verification)
- [Configuration](#%EF%B8%8F-configuration)
- [GitHub Actions Usage](#-github-actions-usage)
- [Troubleshooting](#-troubleshooting)
- [File Structure](#-file-structure)

---

## 🚀 Quick Start

### 1. Fork or clone this repo

```bash
git clone https://github.com/YOUR_USERNAME/live-playlist-generator.git
cd live-playlist-generator
┌─────────────────────────────────────────────────────────────┐
│                   GitHub Actions (every 10 min)             │
│                                                             │
│  ┌───────────┐    ┌──────────────────────────────────────┐  │
│  │ model.txt │───▶│  STEP 1: Fetch Favourite Models      │  │
│  │           │    │                                      │  │
│  │ Model1,   │    │  For each model, try in order:       │  │
│  │ Model2,   │    │   1. Stripchat API                   │  │
│  │ Model3    │    │   2. Chaturbate API                  │  │
│  └───────────┘    │   3. Browser network interception    │  │
│                   │                                      │  │
│                   │  ⬇ Every URL is VERIFIED ⬇           │  │
│                   │  Download .m3u8 → check valid HLS    │  │
│                   └──────────────────────────────────────┘  │
│                              │                              │
│                    ┌─────────▼──────────┐                   │
│                    │ 5+ favs online?    │                   │
│                    └─────────┬──────────┘                   │
│                     YES │          │ NO                      │
│                         │          │                         │
│                         ▼          ▼                         │
│                    ┌────────┐  ┌───────────────────────┐    │
│                    │ SKIP   │  │ STEP 2: Fetch Top 10  │    │
│                    │ Top 10 │  │ from chococams.com    │    │
│                    └────────┘  └───────────────────────┘    │
│                         │          │                         │
│                         ▼          ▼                         │
│                   ┌──────────────────────────────────────┐  │
│                   │  STEP 3: Generate playlists/live.m3u │  │
│                   │  Git commit & push                   │  │
│                   └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
