# Manifest 格式

所有路径使用绝对路径。图片坐标基于原始图片尺寸。

```json
{
  "source_video": "C:\\input\\card.mov",
  "output_dir": "C:\\output\\batch",
  "source_crop": "672:1019:24:129",
  "output_prefix": "门店素材替换",
  "jobs": [
    {
      "index": 1,
      "image": "C:\\input\\scene01.png",
      "audio": "C:\\input\\voice01.wav",
      "screen": [198, 290, 744, 290, 198, 1248, 744, 1248]
    }
  ]
}
```

`screen` 顺序：`左上x, 左上y, 右上x, 右上y, 左下x, 左下y, 右下x, 右下y`。

`source_crop` 为空字符串时使用源视频全部画面；存在模糊延展边时必须填写清晰内容区域。输出时透视变换会把裁切后的内容铺满指定屏幕，不添加任何边框。

如果音频由内置 TTS 生成，先把 wav 写到固定目录，再把生成后的绝对路径填入 `jobs[].audio`。批量任务中每个 `jobs[].audio` 必须指向不同 wav，且这些 wav 应由不同话术逐条合成；可以共用参考音色，但不能多条视频复用同一个成品音频。渲染脚本只读取实际音频文件，不读取配音文案。
