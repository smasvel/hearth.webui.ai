#!/usr/bin/env python3
"""Render the README feature demo and its silent GIF preview from Hearth screenshots."""

from __future__ import annotations

import base64
import math
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
WIDTH, HEIGHT, FPS = 1280, 720, 15
SCENE_SECONDS = 4.2
SCENE_COUNT = 6
DURATION = SCENE_SECONDS * SCENE_COUNT


def image_data(filename: str) -> str:
    data = (OUT / filename).read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def build_page() -> str:
    shots = {
        "models": image_data("Downloads.png"),
        "chat": image_data("chat.png"),
        "agents": image_data("agents.png"),
        "tests": image_data("model-tests.png"),
        "metrics": image_data("metrics.png"),
    }
    import json

    return PAGE.replace("__SHOTS__", json.dumps(shots))


PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box}html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:#090e13;color:#eef5f2;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
body{position:relative;background:radial-gradient(ellipse at 50% 42%,#1a2c31 0,#10191f 40%,#090e13 83%)}
.grain{position:absolute;inset:0;opacity:.12;background-image:radial-gradient(#d6fff21c .7px,transparent .8px);background-size:5px 5px;mask-image:linear-gradient(90deg,#000,transparent 95%)}
.glow{position:absolute;width:640px;height:640px;border-radius:50%;filter:blur(90px);opacity:.1;background:#50e0ae;left:320px;top:80px}
.grid{position:absolute;inset:0;opacity:.22;background-image:linear-gradient(#9bd9cb0c 1px,transparent 1px),linear-gradient(90deg,#9bd9cb0c 1px,transparent 1px);background-size:48px 48px;mask-image:radial-gradient(ellipse at center,#000 15%,transparent 82%)}
.brand{position:absolute;z-index:5;top:34px;left:54px;display:flex;align-items:center;gap:11px;font-size:20px;font-weight:700;letter-spacing:-.7px}.mark{width:34px;height:34px;display:grid;place-items:center;border-radius:11px;color:#092016;background:linear-gradient(145deg,#a3f6d3,#49c999);font-weight:850;font-size:20px;box-shadow:0 0 30px #5ee3af55}.brand small{font-size:9px;color:#81918f;letter-spacing:2px;font-weight:700;margin-left:4px}
.topline{position:absolute;z-index:5;top:49px;right:56px;color:#7e9691;font-size:10px;letter-spacing:2px;font-weight:650}.topline b{color:#7de3bb;font-weight:700}
.slides{position:absolute;inset:0}.slide{position:absolute;inset:0;opacity:0;pointer-events:none}.title{position:absolute;z-index:4;left:76px;top:104px;font-size:13px;text-transform:uppercase;letter-spacing:2.8px;color:#81dbb8;font-weight:750}.subtitle{position:absolute;z-index:4;left:76px;top:133px;color:#9aa9a9;font-size:15px;letter-spacing:.15px}.headline{position:absolute;z-index:4;left:76px;top:235px;width:455px;font-size:56px;line-height:1.02;letter-spacing:-2.7px;font-weight:700}.headline em{font-style:normal;color:#82e5bd}.subhead{position:absolute;z-index:4;left:80px;top:378px;width:395px;color:#a9b6b6;font-size:17px;line-height:1.65}.hero-orbit{position:absolute;left:670px;top:102px;width:620px;height:620px;border:1px solid #79dfb51e;border-radius:50%;transform:scaleY(.66);box-shadow:0 0 0 60px #79dfb508,0 0 0 120px #79dfb505}.hero-orbit:before,.hero-orbit:after{content:"";position:absolute;inset:70px;border:1px dashed #8be5c126;border-radius:50%}.hero-orbit:after{inset:145px;border-style:solid;border-color:#8be5c11c}
.feature-pills{position:absolute;z-index:4;left:78px;bottom:104px;display:flex;gap:9px;flex-wrap:wrap;width:490px}.pill{border:1px solid #b9e8d327;background:#17232ad9;color:#c1d0cc;padding:9px 12px;border-radius:20px;font-size:10px;letter-spacing:.4px;box-shadow:0 5px 24px #0005}.pill i{display:inline-block;width:6px;height:6px;border-radius:50%;background:#6ee0b2;margin:0 7px 1px 0;box-shadow:0 0 8px #69e4b0}
.product-window{position:absolute;z-index:2;left:440px;top:164px;width:760px;height:475px;padding:12px;border:1px solid #d6f5ec25;border-radius:17px;background:linear-gradient(150deg,#26353c,#10171d 45%,#121920);box-shadow:0 35px 95px #000b,0 0 55px #67dcb51c;transform:perspective(1600px) rotateY(-4deg) rotateX(1deg);overflow:hidden}.window-bar{height:24px;display:flex;align-items:center;gap:5px;padding:0 6px 8px;color:#81918e;font-size:8px;letter-spacing:1.3px}.window-bar i{width:6px;height:6px;border-radius:50%;background:#536760}.window-bar span{margin-left:8px}.product-window img{display:block;width:100%;height:calc(100% - 24px);object-fit:cover;object-position:top center;border-radius:7px;border:1px solid #334148}
.orbit-label{position:absolute;z-index:3;padding:11px 14px;border:1px solid #a2e8d027;border-radius:10px;background:#142129ee;color:#d1e3dc;font-size:10px;letter-spacing:.8px;box-shadow:0 12px 34px #0009;white-space:nowrap}.orbit-label b{color:#7ce0b8}.label-a{left:395px;top:251px}.label-b{right:46px;top:275px}.label-c{left:382px;bottom:130px}
.footer{position:absolute;z-index:5;left:56px;right:56px;bottom:30px;height:26px;display:flex;align-items:center;justify-content:space-between;border-top:1px solid #ffffff16;color:#71817f;font-size:9px;letter-spacing:1.6px;padding-top:12px}.footer strong{color:#a6b7b1;font-weight:600}.progress{position:absolute;z-index:8;left:0;bottom:0;height:3px;background:linear-gradient(90deg,#70e6b6,#a9e7ff);box-shadow:0 0 12px #73e6b4}
.bot{position:absolute;z-index:1;width:55px;height:67px;transform-origin:50% 70%;filter:drop-shadow(0 12px 13px #0009)}.bot-head{position:absolute;left:7px;top:5px;width:41px;height:34px;border:1px solid var(--bot,#78e2b7);border-radius:12px;background:linear-gradient(140deg,#2b3b42,#11191f);box-shadow:0 0 22px color-mix(in srgb,var(--bot,#78e2b7) 28%,transparent),inset 0 1px #fff2}.bot-head:before{content:"";position:absolute;width:2px;height:8px;left:19px;top:-9px;background:var(--bot,#78e2b7)}.bot-head:after{content:"";position:absolute;width:7px;height:7px;left:17px;top:-14px;border-radius:50%;background:var(--bot,#78e2b7);box-shadow:0 0 12px var(--bot,#78e2b7)}.bot-eyes{position:absolute;left:10px;top:11px;width:5px;height:6px;border-radius:4px;background:var(--bot,#78e2b7);box-shadow:15px 0 0 var(--bot,#78e2b7)}.bot-mouth{position:absolute;width:8px;height:2px;left:16px;bottom:6px;border-radius:3px;background:var(--bot,#78e2b7)}.bot-body{position:absolute;left:15px;top:40px;width:26px;height:19px;display:grid;place-items:center;border:1px solid color-mix(in srgb,var(--bot,#78e2b7) 55%,transparent);border-radius:7px;background:#17242b;color:var(--bot,#78e2b7);font-size:10px}.bot-leg{position:absolute;top:56px;width:7px;height:8px;border-radius:0 0 4px 4px;background:#60747a}.bot-leg.l{left:17px}.bot-leg.r{right:17px}.bot-glow{position:absolute;left:20px;bottom:-4px;width:16px;height:9px;border-radius:50%;background:#75e7be;filter:blur(6px);opacity:.8}
.scene-card{position:absolute;z-index:4;left:82px;top:477px;width:340px;height:79px;border:1px solid #c6efe22b;border-radius:12px;background:#142128eb;box-shadow:0 16px 42px #0008;padding:14px 16px}.scene-card strong{display:block;font-size:11px;letter-spacing:1px;color:#d8e9e3}.scene-card span{display:block;margin-top:7px;font-size:10px;color:#8fa29d;line-height:1.45}.wave{position:absolute;right:19px;top:24px;display:flex;align-items:center;gap:3px;height:29px}.wave i{width:3px;height:var(--h);border-radius:3px;background:linear-gradient(#b2d9ff,#68ddba);box-shadow:0 0 7px #81e0c2;transform:scaleY(var(--amp,1));transform-origin:center;animation:wave 1s ease-in-out infinite alternate;animation-delay:var(--delay)}
.feature-grid{position:absolute;z-index:4;left:80px;top:485px;display:flex;gap:9px}.feature-chip{border:1px solid #b9e8d327;border-radius:10px;background:#142128ef;padding:12px 14px;color:#d0ded9;font-size:10px;letter-spacing:.4px}.feature-chip small{display:block;margin-top:6px;color:#7d928c;font-size:8px;letter-spacing:1px;text-transform:uppercase}
.closing{position:absolute;z-index:5;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;background:radial-gradient(ellipse at 50% 47%,#1b3436 0,#111b20 44%,transparent 78%)}.closing .mark{width:64px;height:64px;border-radius:19px;font-size:38px;box-shadow:0 0 60px #62e3b767}.closing h2{margin:24px 0 9px;font-size:58px;letter-spacing:-3px}.closing p{margin:0;color:#9eafaa;font-size:16px}.closing .closing-pills{display:flex;gap:10px;margin-top:29px}.closing .pill{background:#152329}
.scene-minions{position:absolute;inset:0;pointer-events:none}.screen-flare{position:absolute;z-index:6;top:0;bottom:0;left:-22%;width:20%;background:linear-gradient(90deg,transparent,#a3f6d833,transparent);transform:skewX(-14deg);opacity:0;animation:flare 4.2s linear infinite}
@keyframes wave{to{transform:scaleY(.3)}}@keyframes flare{0%,10%{left:-22%;opacity:0}15%{opacity:1}48%{opacity:.35}60%,100%{left:125%;opacity:0}}
</style></head><body>
<div class="glow"></div><div class="grid"></div><div class="grain"></div><div class="hero-orbit"></div>
<div class="brand"><span class="mark">H</span><span>hearth<span style="color:#78dfb6">.</span></span><small>LOCAL AI WORKSPACE</small></div>
<div class="topline"><b>PRIVATE BY DESIGN</b> &nbsp;·&nbsp; YOUR MODELS, YOUR RULES</div>
<div class="scene-minions" id="minions"></div><main class="slides" id="slides"></main>
<div class="footer"><strong>HEARTH &nbsp;·&nbsp; YOUR PRIVATE AI WORKSPACE</strong><span>LOCAL MODELS &nbsp;·&nbsp; REAL WORKFLOWS &nbsp;·&nbsp; ONE HOME</span><span id="counter">01 / 06</span></div><div class="progress" id="progress"></div>
<div class="screen-flare"></div>
<script>
const shots=__SHOTS__;
const scenes=[
 {kind:'hero',eyebrow:'A calmer way to work with AI',head:'Your local AI.<br><em>One home.</em>',sub:'Chat, create, research, and build with models running on your own hardware.',pills:['LOCAL MODEL CHAT','PROJECT AGENTS','VOICE & CREATION','PRIVATE BY DESIGN']},
 {kind:'product',title:'Find the right model. Make it yours.',sub:'Search, compare, download, and manage models across supported local runtimes.',shot:'models',labels:['MODEL SEARCH','BACKEND CONTROLS','LOAD · TEST · BUILD'],chip:'LM Studio · Lemonade · vLLM · KoboldCPP · FastFlowLM'},
 {kind:'product',title:'A conversation that keeps up.',sub:'Stream answers, revisit saved chats, use voice, and bring useful context forward.',shot:'chat',labels:['STREAMING CHAT','VOICE IN · VOICE OUT','CROSS-CHAT MEMORY'],chip:'Your assistant stays close to your workflow.'},
 {kind:'product',title:'Let your agents do real project work.',sub:'Follow OpenCode tasks, answer questions, and review saved project progress.',shot:'agents',labels:['PLAN','BUILD','VERIFY'],chip:'Persistent project workspaces · progress you can follow'},
 {kind:'product',title:'Know what your models can do.',sub:'Run practical reasoning, tool-use, and safety evaluations. Keep the reports.',shot:'tests',labels:['MODEL TESTS','SCORED RESULTS','LIVE METRICS'],chip:'Evaluate before putting a model into a workflow.'},
 {kind:'closing',title:'Your models. Your data. Your rules.',sub:'A private AI workspace built to fit the way you work.',pills:['WEB RESEARCH','VOICE TOOLS','IMAGE & VIDEO','TAILSCALE ACCESS','OPEN SOURCE']}
];
const slides=document.getElementById('slides');
function productSlide(s,i){return `<section class="slide" data-scene="${i}"><div class="title">${s.title}</div><div class="subtitle">${s.sub}</div><div class="product-window"><div class="window-bar"><i></i><i></i><i></i><span>HEARTH / ${s.shot.toUpperCase()}</span></div><img src="${shots[s.shot]}"></div><div class="orbit-label label-a"><b>✦</b> &nbsp;${s.labels[0]}</div><div class="orbit-label label-b"><b>●</b> &nbsp;${s.labels[1]}</div><div class="orbit-label label-c"><b>↗</b> &nbsp;${s.labels[2]}</div><div class="scene-card"><strong>${s.labels[0]} &nbsp;·&nbsp; ${s.labels[1]}</strong><span>${s.chip}</span></div></section>`}
slides.innerHTML=scenes.map((s,i)=>s.kind==='hero'?`<section class="slide" data-scene="${i}"><div class="title">${s.eyebrow}</div><h1 class="headline">${s.head}</h1><div class="subhead">${s.sub}</div><div class="feature-pills">${s.pills.map(p=>`<span class="pill"><i></i>${p}</span>`).join('')}</div><div class="orbit-label label-a"><b>01</b> &nbsp; LOCAL MODEL CHAT</div><div class="orbit-label label-b"><b>02</b> &nbsp; REAL PROJECT AGENTS</div><div class="orbit-label label-c"><b>03</b> &nbsp; YOUR PRIVATE WORKSPACE</div></section>`:s.kind==='product'?productSlide(s,i):`<section class="slide" data-scene="${i}"><div class="closing"><span class="mark">H</span><h2>${s.title}</h2><p>${s.sub}</p><div class="closing-pills">${s.pills.map(p=>`<span class="pill"><i></i>${p}</span>`).join('')}</div></div></section>`).join('');
const bots=document.getElementById('minions');
const colors=['#7de3b9','#a9baff','#ffd08e','#d1aaff','#ff9c9c','#8be3dc'];
bots.innerHTML=colors.map((c,i)=>`<div class="bot" data-bot="${i}" style="--bot:${c}"><div class="bot-head"><i class="bot-eyes"></i><b class="bot-mouth"></b></div><div class="bot-body">${['✦','⌘','⚙','✓','✳','↗'][i]}</div><i class="bot-leg l"></i><i class="bot-leg r"></i><i class="bot-glow"></i></div>`).join('');
window.renderAt=(t)=>{const idx=Math.min(scenes.length-1,Math.floor(t/4.2));const local=(t-idx*4.2)/4.2;document.documentElement.style.setProperty('--clock',`${-t}s`);document.querySelectorAll('.slide').forEach((el,i)=>{let opacity=0;if(i===idx)opacity=Math.min(1,local/.12);else if(i===idx-1)opacity=Math.max(0,1-local/.12);el.style.opacity=String(opacity);el.style.transform=`translateY(${(1-opacity)*7}px)`});document.getElementById('counter').textContent=`${String(idx+1).padStart(2,'0')} / 06`;document.getElementById('progress').style.width=`${((t+1/15)/(4.2*6))*100}%`;document.querySelectorAll('.bot').forEach((el,i)=>{const phase=t*.65+i*1.04;const x=18+(i%3)*31+Math.sin(phase)*2.7;const y=19+Math.floor(i/3)*60+Math.cos(phase*1.2)*4;el.style.left=x+'%';el.style.top=y+'%';el.style.transform=`translate(-50%,-50%) rotate(${Math.sin(phase)*7}deg) scale(${i===2||i===4?.83:1})`})};
window.renderAt(0);
</script></body></html>"""


def make_audio(path: Path) -> None:
    sr = 48_000
    n = int(DURATION * sr)
    t = np.arange(n, dtype=np.float64) / sr
    mix = np.zeros(n, dtype=np.float64)

    # Warm sustained chords: Dadd9, Bm7, Gmaj9, Asus4, Em7, Dadd9.
    chords = [
        [146.83, 220.00, 293.66, 329.63],
        [123.47, 185.00, 246.94, 293.66],
        [98.00, 146.83, 196.00, 220.00],
        [110.00, 164.81, 220.00, 246.94],
        [82.41, 123.47, 164.81, 196.00],
        [146.83, 220.00, 293.66, 369.99],
    ]
    for i, chord in enumerate(chords):
        start = i * SCENE_SECONDS
        end = min(DURATION, start + SCENE_SECONDS + 0.6)
        sel = (t >= start) & (t < end)
        local = t[sel] - start
        fade = np.minimum(1, local / 0.55) * np.minimum(1, (end - t[sel]) / 0.8)
        pad = np.zeros(local.size)
        for j, freq in enumerate(chord):
            pad += np.sin(2 * np.pi * freq * local + j * 0.18) / (1 + j * 0.22)
            pad += 0.22 * np.sin(2 * np.pi * freq * 2.01 * local + j) / (1 + j)
        mix[sel] += 0.075 * pad * fade

    # Bright, lightly syncopated pentatonic plucks.
    arp = [587.33, 739.99, 880.00, 1108.73, 880.00, 739.99, 659.25, 987.77]
    step = 0.25
    for k in range(int(DURATION / step)):
        start = k * step
        idx = int(start * sr)
        length = min(int(0.23 * sr), n - idx)
        if length <= 0:
            continue
        u = np.arange(length) / sr
        env = np.exp(-u * 17) * np.minimum(1, u * 100)
        f = arp[k % len(arp)] * (0.5 if k % 8 in (3, 7) else 1)
        pluck = (np.sin(2 * np.pi * f * u) + 0.3 * np.sin(2 * np.pi * f * 2.003 * u)) * env
        mix[idx:idx + length] += 0.065 * pluck

    # Soft cinematic pulse with a low kick and a bright offbeat tick.
    for beat in np.arange(0, DURATION, 0.5):
        idx = int(beat * sr)
        length = min(int(0.32 * sr), n - idx)
        u = np.arange(length) / sr
        env = np.exp(-u * 13)
        freq = 78 - 30 * np.minimum(1, u / 0.14)
        mix[idx:idx + length] += 0.20 * np.sin(2 * np.pi * freq * u) * env
        tick_idx = idx + int(0.25 * sr)
        if tick_idx < n:
            ln = min(int(0.075 * sr), n - tick_idx)
            u2 = np.arange(ln) / sr
            rng = np.random.default_rng(int(beat * 200 + 91))
            noise = rng.normal(0, 1, ln)
            # Short, filtered-feeling noise with a tonal edge.
            mix[tick_idx:tick_idx + ln] += 0.018 * (noise * np.exp(-u2 * 48) + np.sin(2*np.pi*1250*u2)*np.exp(-u2*35))

    # Reverse whooshes into each feature reveal, followed by a two-note robot chirp.
    rng = np.random.default_rng(420)
    noise = rng.normal(0, 1, n)
    for i in range(1, SCENE_COUNT):
        center = i * SCENE_SECONDS
        start = max(0, int((center - 0.72) * sr))
        end = min(n, int((center + 0.08) * sr))
        u = np.arange(end - start) / sr
        sweep = u / max(u[-1], 1e-6)
        env = np.sin(np.pi * sweep / 2) ** 2 * (1 - sweep * 0.35)
        mix[start:end] += 0.07 * noise[start:end] * env * np.sin(2 * np.pi * (270 + 1000 * sweep) * u)
        # Tiny rising, glassy two-note cue at the scene cut.
        for offset, freq in ((0.0, 880), (0.095, 1174.66)):
            idx = int((center + offset) * sr)
            ln = min(int(0.22 * sr), n - idx)
            u2 = np.arange(ln) / sr
            env2 = np.exp(-u2 * 14) * np.minimum(1, u2 * 70)
            mix[idx:idx + ln] += 0.12 * np.sin(2 * np.pi * freq * u2) * env2

    # End on a warm held chord with a smooth master fade.
    fade_out = np.ones(n)
    fade_out[-int(1.1 * sr):] = np.linspace(1, 0, int(1.1 * sr))
    mix *= fade_out
    mix = np.tanh(mix * 1.45) * 0.86
    stereo = np.stack((mix, np.roll(mix, int(0.004 * sr)) * 0.97), axis=1)
    pcm = (stereo * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(sr)
        wav.writeframes(pcm.tobytes())


def render_frames(directory: Path) -> None:
    html = build_page()
    count = int(DURATION * FPS)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, executable_path="/usr/bin/google-chrome",
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        page = browser.new_page(viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1)
        page.set_content(html, wait_until="load")
        page.wait_for_function("[...document.images].every(image => image.complete && image.naturalWidth > 0)")
        for frame in range(count):
            page.evaluate("t => window.renderAt(t)", frame / FPS)
            page.screenshot(
                path=str(directory / f"frame_{frame:05d}.jpg"),
                type="jpeg", quality=91, animations="allow",
            )
        browser.close()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hearth-demo-") as temp:
        tempdir = Path(temp)
        frames = tempdir / "frames"
        frames.mkdir()
        render_frames(frames)
        audio = tempdir / "score.wav"
        make_audio(audio)
        mp4 = OUT / "hearth-feature-demo.mp4"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-framerate", str(FPS), "-i", str(frames / "frame_%05d.jpg"),
            "-i", str(audio), "-vf", "fps=30,format=yuv420p",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart",
            str(mp4),
        ], check=True)
        gif = OUT / "hearth-feature-demo.gif"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(mp4), "-vf",
            "fps=9,scale=480:-1:flags=lanczos,split[s0][s1];"
            "[s0]palettegen=max_colors=64:stats_mode=diff[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle",
            "-loop", "0", str(gif),
        ], check=True)
        print(f"Created {mp4} ({mp4.stat().st_size:,} bytes)")
        print(f"Created {gif} ({gif.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
