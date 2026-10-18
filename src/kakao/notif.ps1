# 윈도우 알림센터에서 '카카오톡' 토스트를 읽어 JSON 배열로 출력.
# Windows PowerShell 5.1 에서 실행해야 함 (pwsh 7 은 WinRT 미지원).
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, $t) { $m = $asTaskGeneric.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }

[Windows.UI.Notifications.Management.UserNotificationListener, Windows.UI.Notifications.Management, ContentType = WindowsRuntime] | Out-Null
[Windows.UI.Notifications.NotificationKinds, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null

$listener = [Windows.UI.Notifications.Management.UserNotificationListener]::Current
$access = Await ($listener.RequestAccessAsync()) ([Windows.UI.Notifications.Management.UserNotificationListenerAccessStatus])
if ("$access" -ne 'Allowed') { Write-Output '[]'; exit }

$notifs = Await ($listener.GetNotificationsAsync([Windows.UI.Notifications.NotificationKinds]::Toast)) ([System.Collections.Generic.IReadOnlyList[Windows.UI.Notifications.UserNotification]])
$result = @()
foreach ($n in $notifs) {
  $app = $n.AppInfo.DisplayInfo.DisplayName
  if ($app -notmatch '카카오|Kakao') { continue }
  $texts = @()
  $binding = $n.Notification.Visual.GetBinding([Windows.UI.Notifications.KnownNotificationBindings]::ToastGeneric)
  if ($binding) { foreach ($e in $binding.GetTextElements()) { $texts += $e.Text } }
  $result += [pscustomobject]@{
    title = if ($texts.Count -ge 1) { $texts[0] } else { '' }
    body  = if ($texts.Count -ge 2) { ($texts[1..($texts.Count - 1)] -join ' ') } else { '' }
    time  = $n.CreationTime.ToString('o')
  }
}
# 5.1 은 단일 객체를 배열로 안 싸므로 직접 조립
$json = ($result | ForEach-Object { $_ | ConvertTo-Json -Compress -Depth 4 }) -join ","
Write-Output ("[" + $json + "]")
