# 高光片段批量生成（不启 Python 也能用）
# 用法：  .\scripts\make_highlights.ps1 -Video .\data\mygame.mp4 -Out .\out\hl -Clips "00:01:12-00:01:16","00:03:40-00:03:45"
#
# 说明：Python 管线（aihoop.highlight）已经会自动生成高光；
# 这个脚本是给「手工剪几条」或「ffmpeg 调试」用的。
# 需要 ffmpeg： winget install Gyan.FFmpeg

param(
    [Parameter(Mandatory = $true)][string]$Video,
    [string]$Out = "out/hl",
    [string[]]$Clips = @(),
    # 加比分字幕： -Subtitle "主队 42:38 客队"
    [string]$Subtitle = ""
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Error "未找到 ffmpeg。安装： winget install Gyan.FFmpeg"
}

if (-not (Test-Path $Video)) { Write-Error "视频不存在：$Video" }
New-Item -ItemType Directory -Force -Path $Out | Out-Null

if ($Clips.Count -eq 0) {
    Write-Host "未提供 -Clips，先打印视频信息与环境设置建议：" -ForegroundColor Yellow
    ffprobe -v error -select_streams v:0 `
        -show_entries stream=width,height,r_frame_rate,nb_frames `
        -show_entries format=duration -of default=noprint_wrappers=1 $Video
    Write-Host "`n用法示例：" -ForegroundColor Cyan
    Write-Host '  .\scripts\make_highlights.ps1 -Video .\data\game.mp4 -Clips "00:01:12-00:01:16"'
    exit 0
}

$i = 0
foreach ($c in $Clips) {
    $parts = $c -split "-"
    if ($parts.Count -ne 2) { Write-Warning "跳过格式不对的片段：$c"; continue }
    $start = $parts[0].Trim(); $end = $parts[1].Trim()
    $dst = Join-Path $Out ("clip_{0:d2}.mp4" -f $i)

    # 重编码保证帧精确起止（-ss 放在 -i 前面是快速定位，精度略差；
    # 这里放在 -i 后面做精确定位，代价是要解码到该位置）
    $args = @("-y", "-i", $Video, "-ss", $start, "-to", $end,
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
              "-c:a", "aac", "-movflags", "+faststart")

    if ($Subtitle -ne "") {
        # 字幕：白字 + 黑描边，底部居中
        $vf = "drawtext=text='$Subtitle':fontsize=48:fontcolor=white:" +
              "borderw=3:bordercolor=black:x=(w-text_w)/2:y=h-th-60"
        $args += @("-vf", $vf)
    }
    $args += $dst

    Write-Host "剪辑 $start -> $end : $dst" -ForegroundColor Cyan
    & ffmpeg @args
    if ($LASTEXITCODE -ne 0) { Write-Warning "片段 $c 剪辑失败" }
    $i++
}

# 拼成合集
$list = Join-Path $Out "concat.txt"
$clips = Get-ChildItem $Out -Filter "clip_*.mp4" | Sort-Object Name
if ($clips.Count -gt 0) {
    $clips | ForEach-Object { "file '$($_.FullName -replace '\\','/')'" } |
        Set-Content -Encoding UTF8 $list
    $reel = Join-Path $Out "highlights_reel.mp4"
    Write-Host "拼接合集 -> $reel" -ForegroundColor Cyan
    ffmpeg -y -f concat -safe 0 -i $list -c copy $reel
}

Write-Host "`n完成，产物在 $Out" -ForegroundColor Green
