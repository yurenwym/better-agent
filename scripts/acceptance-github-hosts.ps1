param([switch]$Restore)
$ErrorActionPreference = 'Stop'
$target = 'C:\Windows\System32\drivers\etc\hosts'
$backup = 'D:\RAG\better\data\logs\hosts-before-acceptance-github'
$text = [System.IO.File]::ReadAllText($target)
if ($Restore) {
    $updated = [regex]::Replace($text, '(?m)^# ACCEPTANCE-TEMP (127\.0\.0\.1[ \t]+(?:github\.com|raw\.githubusercontent\.com)[ \t]*)\r?\n?', {
        param($match)
        $original = $match.Groups[1].Value
        if ([regex]::IsMatch($text, '(?m)^' + [regex]::Escape($original) + '\r?$')) { return '' }
        return $original + "`r`n"
    })
} else {
    if (-not (Test-Path -LiteralPath $backup)) { Copy-Item -LiteralPath $target -Destination $backup }
    $updated = $text -replace '(?m)^(127\.0\.0\.1[ \t]+(?:github\.com|raw\.githubusercontent\.com)[ \t]*\r?)$', '# ACCEPTANCE-TEMP $1'
}
if ($updated -ne $text) {
    [System.IO.File]::WriteAllText($target, $updated, [System.Text.UTF8Encoding]::new($false))
    Clear-DnsClientCache
}
