# 有道 TTS 用法

内置脚本 `scripts/youdao_tts.py` 用有道 `confucius4-tts` Gradio 接口把文案合成为 wav。它是声音克隆接口，参考音频必填；合成时会把参考音频与文本上传到 `confucius4-tts.youdao.com`。

## 依赖

- Python 3.10+
- `httpx`
- 需要从 mp4/mp3/m4a/wav 抽参考音色时，还需要 `ffmpeg`

如当前 Python 没有 `httpx`，先在本地环境安装后再运行脚本。

## 抽取参考音色

从用户提供的人声视频或音频中截取 3-10 秒干净单人语音：

```bash
python scripts/youdao_tts.py \
  --prepare-ref "/absolute/reference.mp4" \
  --ref-start 0 \
  --ref-dur 10 \
  --set-default
```

stdout 最后一行是抽出的 wav 绝对路径。`--set-default` 会写入 `~/.youdao-tts/config.json`，后续合成可以省略 `--ref`。

## 合成单条配音

```bash
python scripts/youdao_tts.py \
  --text "要合成的配音文案" \
  --ref "/absolute/ref.wav" \
  --out "/absolute/audio/voice01.wav"
```

常用参数：

- `--lang`：默认 `zh`，也支持 `en/ja/ko/de/fr/th/id/vi/es/pt/it/ru/ms`
- `--name`：不指定 `--out` 时使用的文件名 slug
- `--show-config`：查看默认参考音频配置
- `--probe`：探测服务端配置

## 用在素材替换视频里

1. 为每条视频写一条不同文案，并分别生成一个独立 wav；不要多条视频复用同一个合成结果。
2. 用 `ffprobe` 检查每个 wav 有有效音频流和正确时长。
3. 将 wav 的绝对路径填到 manifest 的 `jobs[].audio`。
4. 继续运行 `scripts/render_tablet_batch.ps1` 批量渲染。

不要把配音文案直接写进 manifest；渲染脚本只接受真实音频文件路径。参考音色可以相同，最终成片音频必须逐条不同。
