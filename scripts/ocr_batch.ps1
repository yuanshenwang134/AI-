# 批量 OCR：对目录下的图片逐张用 Windows.Media.Ocr 识别，输出 JSON
<<<<<<< HEAD
# 用法: powershell -File ocr_batch.ps1 -Dir <图片目录> -Out <结果json> [-WithWords]
# 为什么要批量：PowerShell 每次启动要几百毫秒，逐帧调用会慢十倍以上。
# -WithWords：额外输出每个词的坐标（words:[{text,x,y,w,h}]）。
#   自动定位给的框常常比真实比分条大一圈（实测 game_04：[384,64,1152,172] vs 真实
#   [600,100,1360,180]），命中率因此从 100% 掉到 79%；有了词坐标才能把框收紧。
#   默认不加这个开关 —— 老的调用方与单测只认 file/text 两个字段。
param(
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][string]$Out,
    [switch]$WithWords
=======
# 用法: powershell -File ocr_batch.ps1 -Dir <图片目录> -Out <结果json>
# 为什么要批量：PowerShell 每次启动要几百毫秒，逐帧调用会慢十倍以上。
param(
    [Parameter(Mandatory = $true)][string]$Dir,
    [Parameter(Mandatory = $true)][string]$Out
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
        $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime] | Out-Null

# 记分牌是数字+英文队名，用英文引擎更准；拿不到就退回用户语言
$engine = $null
try {
    $lang = New-Object Windows.Globalization.Language 'en-US'
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
} catch { }
if ($null -eq $engine) {
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
}
if ($null -eq $engine) { Write-Output '{"error":"NO_OCR_ENGINE"}'; exit 1 }

$results = New-Object System.Collections.Generic.List[object]
$files = Get-ChildItem -Path $Dir -Filter *.png | Sort-Object Name
foreach ($f in $files) {
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($f.FullName)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $res = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
<<<<<<< HEAD
        if ($WithWords) {
            # 每个词的包围盒（ocr_batch 以前只回文本，词坐标被丢掉）
            $words = New-Object System.Collections.Generic.List[object]
            foreach ($line in $res.Lines) {
                foreach ($w in $line.Words) {
                    $r = $w.BoundingRect
                    $words.Add([pscustomobject]@{
                        text = $w.Text
                        x    = [int][math]::Round($r.X)
                        y    = [int][math]::Round($r.Y)
                        w    = [int][math]::Round($r.Width)
                        h    = [int][math]::Round($r.Height)
                    })
                }
            }
            $results.Add([pscustomobject]@{ file = $f.Name; text = $res.Text; words = $words })
        } else {
            $results.Add([pscustomobject]@{ file = $f.Name; text = $res.Text })
        }
=======
        $results.Add([pscustomobject]@{ file = $f.Name; text = $res.Text })
>>>>>>> a85d267883743e2b264c8702f6b94fcd5f78480f
        $stream.Dispose()
    } catch {
        $results.Add([pscustomobject]@{ file = $f.Name; text = ""; error = "$($_.Exception.Message)" })
    }
}

$json = $results | ConvertTo-Json -Depth 4 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding($false)))
Write-Output ("OCR 完成: " + $results.Count + " 张 → " + $Out)
