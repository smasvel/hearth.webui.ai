# Pi Assistant - a private, local "Alexa" for your home

Wake word -> speech-to-text -> brain (rules + local LLM) -> Home Assistant -> spoken reply.
Everything runs on your own hardware. No cloud microphone, no subscriptions.

## Hardware (~$100 for the brain box)
| Part | Example | Price |
|---|---|---|
| Computer | Raspberry Pi 5 (8 GB recommended for the LLM) | ~$80 |
| Microphone | ReSpeaker 2-Mic HAT or any USB mic | ~$10-25 |
| Speaker | 3 W speaker + MAX98357A I2S amp (or USB speaker) | ~$12 |
| Radio (optional) | SONOFF ZBDongle-E for Zigbee bulbs/plugs/sensors | ~$20 |

## Install (Raspberry Pi OS 64-bit)
    sudo apt update
    sudo apt install -y python3-pip git portaudio19-dev libasound2-dev piper
    curl -fsSL https://ollama.com/install.sh | sh
    ollama pull qwen2.5:3b

    cd ~ && git clone <your-repo> pi-assistant && cd pi-assistant
    pip install -r requirements.txt
    python -m openwakeword.utils.download_models   # wake-word models

1. Edit config.yaml: rooms -> your entity IDs.
2. Home Assistant: Profile -> Security -> Long-Lived Access Token -> paste into config.
3. Test the brain with no hardware: `python test_offline.py`
4. Run it: `python main.py`

## Auto-start on boot
    sudo tee /etc/systemd/system/pi-assistant.service <<EOF
    [Unit]
    Description=Pi Assistant
    After=network-online.target sound.target
    [Service]
    WorkingDirectory=/home/pi/pi-assistant
    ExecStart=/usr/bin/python3 main.py
    Restart=always
    User=pi
    [Install]
    WantedBy=multi-user.target
    EOF
    sudo systemctl enable --now pi-assistant
