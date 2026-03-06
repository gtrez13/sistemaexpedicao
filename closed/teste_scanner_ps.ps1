$BASE = "http://127.0.0.1:5000"
$SKU  = "17565121485"  # troca por um SKU que exista em SEPARADO

$body = @{ sku = $SKU } | ConvertTo-Json

try {
  $res = Invoke-RestMethod -Method Post -Uri "$BASE/scanner/bipar" -ContentType "application/json" -Body $body
  "✅ Resposta:"
  $res | ConvertTo-Json -Depth 10
}
catch {
  # pega o body do erro (JSON) e mostra
  $errResp = $_.Exception.Response
  if ($errResp -and $errResp.GetResponseStream()) {
    $reader = New-Object System.IO.StreamReader($errResp.GetResponseStream())
    $text = $reader.ReadToEnd()
    "⚠️ Resposta (mesmo com erro HTTP):"
    $text
  } else {
    "❌ Falhou: $($_.Exception.Message)"
  }
}
