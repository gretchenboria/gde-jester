# The Royal GDE Jester 🃏

Welcome, my liege, to **The Royal GDE Jester**—your personal, 3D-animated trickster built to help you juggle the exhausting responsibilities and perks of being a Google Developer Expert (GDE).

Deployed on the **Gemini Enterprise Agent Platform** (Agent Engine) and served via **Google Cloud Run**, this agent manages your developer workload with elegance, wit, and entirely too much sarcasm.

## ✨ Features

- **Juggling 3D UI**: An elegant, vintage WebGL interface featuring a trickster who juggles your "GDE Homework" in real-time.
- **Agentic Workflows**: 
  - Automatically fetches and parses EAP / GDE emails via the Gmail API.
  - Automatically pushes deadlines to your Google Calendar.
  - Monitors your GCP Cloud Billing credits.
  - Natively copies Colab notebooks from shared links directly into your Drive.
  - Logs your community activities directly to **Advocu**.
- **Gemini Omni Integration (The "Royal Omni-Vision")**: Powered by `gemini-3.8-omni`. Hold up your physical conference badges, handwritten networking notes, or new GDE swag to your webcam. The Jester natively processes the visual/audio stream, critiques your swag with snarky humor, and automatically logs the event to your Advocu activity stream.
- **Native TTS**: The Jester speaks back to you using native `gemini-3.8-flash-tts` audio modalities (using the 'Puck' persona).

## 🚀 Deployment

The project is scaffolded using Google `agents-cli`.

1. **Install Dependencies**:
   ```bash
   uv sync
   uv add google-api-python-client
   ```

2. **Authenticate**:
   Ensure you have your Google Cloud credentials and Gemini API Key configured in your environment. DO NOT check in `.env` files.

3. **Deploy to Cloud Run**:
   ```bash
   agents-cli deploy --project <YOUR_GCP_PROJECT>
   ```

## 🎭 The Creative Use of Gemini Omni

Why just chat when your agent can *see*? We integrated `gemini-3.8-omni` to provide **multimodal event logging**. 

Instead of typing out a report for your annual GDE renewal, simply hold your conference badge or speaker lanyard up to the camera. The Omni model visually parses the event name, infers the dates, verbally congratulates (or mocks) you on the quality of the badge, and automatically fires off the `log_advocu_activity` tool to keep your profile updated. It's the ultimate hands-free administrative assistant for overworked experts.
