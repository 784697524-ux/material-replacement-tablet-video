param([Parameter(Mandatory=$true)][string]$Manifest)

$ErrorActionPreference = 'Stop'
$config = Get-Content -LiteralPath $Manifest -Raw -Encoding UTF8 | ConvertFrom-Json
$source = [string]$config.source_video
$root = [string]$config.output_dir
$prefix = [string]$config.output_prefix
$crop = [string]$config.source_crop
$jobs = @($config.jobs)

if (-not (Test-Path -LiteralPath $source)) { throw "Source video not found: $source" }
if ($jobs.Count -eq 0) { throw 'Manifest contains no jobs.' }
if ([string]::IsNullOrWhiteSpace($prefix)) { $prefix = '素材替换视频' }
New-Item -ItemType Directory -Force -Path $root, (Join-Path $root '封面截图'), (Join-Path $root '遮罩') | Out-Null
Add-Type -AssemblyName System.Drawing

foreach ($job in $jobs) {
    if (-not (Test-Path -LiteralPath $job.image)) { throw "Image not found: $($job.image)" }
    if (-not (Test-Path -LiteralPath $job.audio)) { throw "Audio not found: $($job.audio)" }
    $p = @($job.screen)
    if ($p.Count -ne 8) { throw "Job $($job.index) screen must contain 8 coordinates." }
    $image = [System.Drawing.Image]::FromFile([string]$job.image)
    $width = $image.Width
    $height = $image.Height
    $image.Dispose()

    $maskPath = Join-Path $root ('遮罩\screen_{0:D2}.png' -f [int]$job.index)
    $bmp = New-Object System.Drawing.Bitmap $width,$height
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.Clear([System.Drawing.Color]::Black)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $points = [System.Drawing.Point[]]@(
        (New-Object System.Drawing.Point $p[0],$p[1]),
        (New-Object System.Drawing.Point $p[2],$p[3]),
        (New-Object System.Drawing.Point $p[6],$p[7]),
        (New-Object System.Drawing.Point $p[4],$p[5])
    )
    $g.FillPolygon([System.Drawing.Brushes]::White, $points)
    $bmp.Save($maskPath, [System.Drawing.Imaging.ImageFormat]::Png)
    $g.Dispose(); $bmp.Dispose()

    $duration = [double](& ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 $job.audio)
    $sourceFilter = if ([string]::IsNullOrWhiteSpace($crop)) { "[1:v]scale=${width}:${height}" } else { "[1:v]crop=$crop,scale=${width}:${height}" }
    $filter = "$sourceFilter,perspective=x0=$($p[0]):y0=$($p[1]):x1=$($p[2]):y1=$($p[3]):x2=$($p[4]):y2=$($p[5]):x3=$($p[6]):y3=$($p[7]):sense=destination:interpolation=linear[vid];[2:v]format=gray,gblur=sigma=0.7[mask];[vid][mask]alphamerge[screen];[0:v][screen]overlay=0:0:format=auto,scale=1080:1920[outv]"
    $videoPath = Join-Path $root ('{0}_{1:D2}.mp4' -f $prefix,[int]$job.index)
    $coverPath = Join-Path $root ('封面截图\{0}_{1:D2}_封面.jpg' -f $prefix,[int]$job.index)
    & ffmpeg -hide_banner -loglevel error -loop 1 -framerate 30 -i $job.image -stream_loop -1 -i $source -loop 1 -framerate 30 -i $maskPath -i $job.audio -filter_complex $filter -map '[outv]' -map 3:a:0 -t $duration -r 30 -c:v libx264 -preset medium -crf 19 -pix_fmt yuv420p -c:a aac -b:a 192k -movflags +faststart -y $videoPath
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg render failed for job $($job.index)" }
    & ffmpeg -hide_banner -loglevel error -ss ([Math]::Min(2, $duration / 2)) -i $videoPath -frames:v 1 -q:v 2 -y $coverPath
    if ($LASTEXITCODE -ne 0) { throw "cover extraction failed for job $($job.index)" }
    Write-Output ('rendered {0}/{1}' -f $job.index,$jobs.Count)
}

$covers = @($jobs | ForEach-Object { Join-Path $root ('封面截图\{0}_{1:D2}_封面.jpg' -f $prefix,[int]$_.index) })
$tw=188; $th=334; $columns=[Math]::Min(5,$covers.Count); $rows=[Math]::Ceiling($covers.Count/$columns)
$sheet = New-Object System.Drawing.Bitmap ($columns*$tw),($rows*$th)
$sg = [System.Drawing.Graphics]::FromImage($sheet); $sg.Clear([System.Drawing.Color]::Black)
for ($i=0; $i -lt $covers.Count; $i++) {
    $cover=[System.Drawing.Image]::FromFile($covers[$i])
    $sg.DrawImage($cover,(($i%$columns)*$tw),([Math]::Floor($i/$columns)*$th),$tw,$th)
    $cover.Dispose()
}
$sheet.Save((Join-Path $root ('01-{0}验收拼图.jpg' -f $covers.Count)),[System.Drawing.Imaging.ImageFormat]::Jpeg)
$sg.Dispose(); $sheet.Dispose()

$videos=@(Get-ChildItem -LiteralPath $root -Filter '*.mp4')
$madeCovers=@(Get-ChildItem -LiteralPath (Join-Path $root '封面截图') -Filter '*.jpg')
if ($videos.Count -ne $jobs.Count -or $madeCovers.Count -ne $jobs.Count) { throw 'Output count verification failed.' }
foreach ($video in $videos) {
    $probe=& ffprobe -v error -show_entries stream=codec_type,width,height -of json $video.FullName | ConvertFrom-Json
    $vs=@($probe.streams|Where-Object codec_type -eq 'video')[0]
    $as=@($probe.streams|Where-Object codec_type -eq 'audio')[0]
    if ($vs.width -ne 1080 -or $vs.height -ne 1920 -or -not $as) { throw "Media verification failed: $($video.Name)" }
}
Write-Output "verified $($videos.Count) videos and $($madeCovers.Count) covers"
