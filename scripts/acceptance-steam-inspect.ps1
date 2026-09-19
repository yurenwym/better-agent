param([string]$SelectName)
$ErrorActionPreference = 'Stop'
trap { $_ | Out-String | Out-File -LiteralPath 'D:\RAG\better\data\logs\acceptance-steam-error.log' -Encoding utf8; exit 1 }
$rows = Get-CimInstance Win32_Process -Filter "Name LIKE 'Steam%'" | Select-Object ProcessId,Name,ExecutablePath,CommandLine
$rows | ConvertTo-Json | Out-File -LiteralPath 'D:\RAG\better\data\logs\acceptance-steam-processes.json' -Encoding utf8
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children,[System.Windows.Automation.Condition]::TrueCondition)
$items = foreach ($window in $windows) {
    if ($window.Current.ProcessId -in $rows.ProcessId) {
        if ($SelectName) {
            $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,$SelectName)
            $element = $window.FindFirst([System.Windows.Automation.TreeScope]::Descendants,$condition)
            if ($null -eq $element) { throw 'Control not found' }
            $patterns = $element.GetSupportedPatterns()
            if ($patterns.Id -contains [System.Windows.Automation.SelectionItemPattern]::Pattern.Id) {
                $element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
            } elseif ($patterns.Id -contains [System.Windows.Automation.InvokePattern]::Pattern.Id) {
                $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            } else { throw ('Control does not support selection or invocation: ' + ($patterns.ProgrammaticName -join ', ')) }
        }
        $children = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,[System.Windows.Automation.Condition]::TrueCondition)
        [pscustomobject]@{ Name=$window.Current.Name; ProcessId=$window.Current.ProcessId; Children=@($children | ForEach-Object { [pscustomobject]@{ Name=$_.Current.Name; Id=$_.Current.AutomationId; Type=$_.Current.ControlType.ProgrammaticName } }) }
    }
}
$items | ConvertTo-Json -Depth 5 | Out-File -LiteralPath 'D:\RAG\better\data\logs\acceptance-steam-ui.json' -Encoding utf8
