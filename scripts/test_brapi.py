import urllib.request
import json
try:
    url = "https://brapi.dev/api/options/BOVA11"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    html = urllib.request.urlopen(req).read()
    data = json.loads(html)
    print("Sucesso! Keys:", data.keys())
    if 'options' in data:
        print("Opções encontradas:", len(data['options']))
        print("Amostra:", data['options'][0])
except Exception as e:
    print("Erro API BRAPI:", e)
