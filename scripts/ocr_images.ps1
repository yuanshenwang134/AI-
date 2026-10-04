param(
    [Parameter(Mandatory=$true)][string]$ImageDir,
    [string]$Pattern = "*.png",
    [Parameter(Mandatory=$true)][string]$OutJson
)
# Windows OCR (Windows.Media.Ocr) based image reader.
# Called from Python; outputs JSON: [{"file": "...", "text": "..."}, ...]
$ErrorActionPreference = "Stop"
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

[void][Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
[void][Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if (-not $engine) {
    try {
        $lang = New-Object Windows.Globalization.Language("zh-Hans-CN")
        $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
    } catch { $engine = $null }
}
if (-not $engine) {
    '[]' | Out-File -FilePath $OutJson -Encoding utf8
    exit 0
}

$items = @()
Get-ChildItem -Path $ImageDir -Filter $Pattern | Sort-Object Name | ForEach-Object {
    $path = $_.FullName
    try {
        $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
        $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        $result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
        $stream.Dispose()
        $words = @()
        foreach ($line in $result.Lines) {
            foreach ($word in $line.Words) {
                $r = $word.BoundingRect
                $words += [ordered]@{
                    text = $word.Text
                    x = [math]::Round($r.X, 1)
                    y = [math]::Round($r.Y, 1)
                    w = [math]::Round($r.Width, 1)
                    h = [math]::Round($r.Height, 1)
                }
            }
        }
        $items += [ordered]@{ file = $_.Name; text = $result.Text; words = $words }
    } catch {
        $items += [ordered]@{ file = $_.Name; text = "" }
    }
}
$json = ConvertTo-Json -InputObject $items -Depth 4 -Compress
[System.IO.File]::WriteAllText($OutJson, $json, (New-Object System.Text.UTF8Encoding($false)))
