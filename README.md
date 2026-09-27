# IDX Stock Intelligence v0.1

PWA + FastAPI untuk screening saham BEI berdasarkan technical, bandarmology, foreign flow, fundamental, event intelligence 6 bulan, prediction journal, dan evaluasi otomatis.

## Prinsip data
Aplikasi sengaja **tidak melakukan scraping endpoint broker/IDX yang tidak didokumentasikan**. Gunakan feed berlisensi/ekspor resmi. IDX Data Services menyediakan EoD, Recapitulation (foreign buy/sell), real-time market depth, dan Data Reference (financial statements/corporate actions/material disclosures).

## Jalankan
```bash
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8765
```
Buka `http://127.0.0.1:8765`.

## Otomasi
Scheduler berjalan Senin-Jumat 16:20 Asia/Jakarta. Ia membaca folder `data/imports/`, menjalankan evaluasi prediksi, dan endpoint `/api/run-eod` dapat membuat prediksi baru untuk setup kuat.

Untuk server/cloud, mount folder feed ke `IDX_IMPORT_DIR`.

## Format CSV
### prices.csv
`symbol,date,open,high,low,close,volume,value,frequency`

### foreign.csv
`symbol,date,foreign_buy,foreign_sell,domestic_buy,domestic_sell`

### brokers.csv
`symbol,date,broker,buy_value,sell_value`

### fundamentals.csv
`symbol,period,revenue,net_profit,equity,debt,ocf,eps,shares,source`

### events.csv
`symbol,event_time,category,title,source,url,impact_hint`

`impact_hint`: positive / negative / neutral / unknown. Skor event **bukan** klaim kausal; nanti harus dikalibrasi dengan event-study harga aktual.

## Yang sudah bekerja
- Technical score: SMA20/SMA50, RSI14, volume ratio.
- Foreign: net 1D/5D/20D/60D.
- Bandarmology: agregasi net broker 20 hari.
- Fundamental: ROE, DER, growth laba, OCF sederhana.
- Event Intelligence: timeline 6 bulan.
- Composite score dan bias.
- Prediksi dengan entry/target/invalidation dan timestamp yang terkunci.
- Evaluasi H+N dan performance dashboard.
- PWA responsive untuk iPhone/Mac/Windows.

## Langkah integrasi LIVE
1. **IDX EoD / Professional** → map ke `prices.csv` atau buat adapter parser format licensee.
2. **IDX Equity EoD Recapitulation** → map foreign buy/sell ke `foreign.csv`.
3. **Broker** → jika broker menyediakan API/ekspor resmi, map broker summary ke `brokers.csv`.
4. **IDX Data Reference** → financial statements + corporate action + material disclosure ke fundamentals/events.
5. Untuk real-time, tambahkan adapter feed IDX real-time 5 Level Market Depth; jangan polling halaman publik.

## Catatan penting
Model v0.1 adalah rule-based dan sengaja transparan. Jangan menganggap skor atau bias sebagai rekomendasi beli/jual. Setelah terkumpul cukup audit trail, bobot harus dikalibrasi dari hasil out-of-sample, bukan dari hindsight.
