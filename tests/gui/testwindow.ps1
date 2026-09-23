# A disposable window for exercising the real GUI tier. Every event it receives
# is appended to the log given as -Log, so a test asserts on what actually
# arrived rather than on what the driver believes it sent.
param([Parameter(Mandatory)][string]$Log, [int]$Seconds = 300, [string]$Title = 'Minos GUI Test')

Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()

function Write-Event([string]$text) { Add-Content -LiteralPath $Log -Value $text -Encoding UTF8 }

$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Size = New-Object System.Drawing.Size(520, 300)
$form.StartPosition = 'CenterScreen'
$form.TopMost = $true

$body = New-Object System.Windows.Forms.TextBox
$body.AccessibleName = 'Body'
$body.Location = New-Object System.Drawing.Point(20, 20)
$body.Size = New-Object System.Drawing.Size(460, 24)
$form.Controls.Add($body)

function Add-Button([string]$label, [int]$x, [int]$y, [string]$event) {
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $label
    $b.Location = New-Object System.Drawing.Point($x, $y)
    $b.Size = New-Object System.Drawing.Size(140, 32)
    $b.Add_Click({ Write-Event ("{0}|{1}" -f $event, $body.Text) }.GetNewClosure())
    $form.Controls.Add($b)
}

# Two controls with the same name, on purpose: the grounding layer must refuse
# to pick one.
Add-Button 'Save' 20 70 'save-draft'
Add-Button 'Save' 180 70 'save-final'
Add-Button 'Save as copy' 340 70 'save-copy'
Add-Button 'Clear' 20 120 'clear'

$label = New-Object System.Windows.Forms.Label
$label.Text = 'Status: ready'
$label.Location = New-Object System.Drawing.Point(20, 170)
$label.Size = New-Object System.Drawing.Size(300, 24)
$form.Controls.Add($label)

$body.Add_KeyDown({ if ($_.Control -and $_.KeyCode -eq 'S') { Write-Event ("ctrl+s|{0}" -f $body.Text) } })
$form.Add_Shown({ $form.Activate(); $body.Focus(); Write-Event 'shown|' })

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = $Seconds * 1000
$timer.Add_Tick({ $form.Close() })
$timer.Start()

[void]$form.ShowDialog()
Write-Event 'closed|'
