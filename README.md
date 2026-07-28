# iTop → SOC Activity Dashboard Sync Agent

Sync **satu arah**: ticket iTop (UserRequest + Incident) otomatis jadi/meng-update
activity di [dashboard](https://dashboard.defendit360-cloud.sbs). Jalan di PC bridge
**Windows** (10.180.71.4) karena iTop (10.110.0.24) tidak punya akses internet.
(Development di Mac: semua perintah sama, ganti `.venv\Scripts\` → `.venv/bin/`.)

```
iTop (10.110.0.24)  ──core/get tiap 60s──▶  agent ini (PC)  ──HTTPS + X-API-Key──▶  /api/integrations/itop  ──▶  Supabase activities
```

## Perilaku penting

- **Upsert by `ticket_itop`** (ref iTop, mis. `R-000123`) — tidak pernah bikin duplikat.
- **Mapping status**: new/assigned/… → `In Progres`, pending → `Pending`,
  resolved/closed/rejected → `Close`. Priority 1-4 → Critical/High/Medium/Minor.
  Class: Incident → `INC`, UserRequest → `SR`.
- **`OVERWRITE_MODE=full`** (default, pilihan operasional): saat ticket iTop berubah,
  SEMUA field data activity ditimpa nilai iTop — field yang iTop tidak punya
  (IP, MITRE, action, escalation, dll) ikut **di-reset**. Ganti ke `partial` di
  `.env` kalau mau isian analyst dipertahankan.
- **HWM (high-water mark)** `last_update` per class disimpan di `sync_state.db` —
  hanya maju setelah dashboard ACK. PC mati / dashboard down → data nyusul otomatis.
- Org iTop yang tidak ada di `clients_map.yaml` → di-skip + WARN (tidak nebak client).

## Setup

### 1. iTop (10.110.0.24)
1. Buat user lokal `rest_sync` (Admin > User Accounts), password kuat.
2. Profile: **REST Services User** + **Support Agent** (read ticket).
   Kode agent ini tidak punya create/update sama sekali → efektif read-only.
3. Tes dari PC: `python itop_client.py` → harus print `✓ core/get tersedia`.

### 2. Dashboard
1. Generate key: `openssl rand -hex 32`.
2. Buat user bot via Admin > Users: nama `iTop Sync Bot`, role `l1`,
   group `DEFENDIT` → copy UUID profile-nya.
3. Di host Proxmox, tambah ke `.env.local`:
   ```
   ITOP_SYNC_API_KEY=<hasil openssl>
   ITOP_SYNC_AGENT_ID=<uuid bot>
   ```
   lalu `docker compose up -d --build`.
4. Pastikan tiap org iTop punya baris di tabel `clients` dashboard,
   lalu isi mapping-nya di `clients_map.yaml`.

### 3. PC bridge Windows (folder ini)
Buka Command Prompt / PowerShell di folder ini:
```bat
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env       & rem isi ITOP_PASSWORD & DASHBOARD_API_KEY
.venv\Scripts\python sync.py --seed              & rem sekali, set titik mulai
.venv\Scripts\python sync.py --once --dry-run    & rem cek mapping tanpa push
.venv\Scripts\python sync.py --loop              & rem jalan beneran
```

### 4. Jalan permanen di Windows (Task Scheduler)
Loop dijalankan lewat `run_sync.bat` (sudah ada di folder ini — auto-restart
30 detik kalau script mati).

**Opsi A — Task Scheduler (utama, survive reboot):**
```bat
schtasks /Create /TN "iTop Sync" /TR "\"C:\path\ke\parser\run_sync.bat\"" /SC ONLOGON /RL LIMITED
```
Lalu di Task Scheduler GUI (`taskschd.msc`) > task "iTop Sync" > Properties:
- General: centang **Run whether user is logged on or not**
- Settings: centang **If the task fails, restart every 1 minute**, matikan
  "Stop the task if it runs longer than"

**Opsi B — paling simpel:** `Win+R` → `shell:startup` → taruh shortcut
`run_sync.bat` di situ (jalan tiap login).

⚠️ PC harus tetap bangun: Settings > System > Power > screen & sleep = **Never**
saat plugged in, atau via terminal (admin):
```bat
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
```
Cek log berjalan: `type logs\itop_sync.log` (atau buka di Notepad).

## Sync periode — pencocokan laporan BiWeekly

Loop delta (HWM) hanya membawa tiket yang berubah SEJAK `--seed` — tiket lama
yang masuk periode laporan bisa belum ada di cache dashboard. Untuk pencocokan
laporan, tarik SEMUA tiket periode (by `start_date`, HWM tidak disentuh):

```bat
rem manual dari PC (mis. periode laporan 20 Jul - 27 Jul, hanya klien AF):
.venv\Scripts\python sync.py --period 2026-07-20 2026-07-27 --client AF

rem batas boleh pakai jam juga (tanda kutip wajib):
.venv\Scripts\python sync.py --period "2026-07-20 08:00" "2026-07-27 17:30" --client AF
```

Otomatis dari dashboard: tombol **Sync iTop (via PC)** di halaman Generate
Laporan membuat antrian `itop_sync_requests`; selama `sync.py --loop` jalan
(via `run_sync.bat` / Task Scheduler),
tiap cycle agent ini mem-poll `GET /api/integrations/itop/sync-requests`,
mengeksekusi permintaan (fetch periode → push cache), lalu PATCH statusnya
(`running` → `done`/`error` + stats). UI dashboard menunggu status itu.
Syaratnya cuma satu: **loop harus jalan di PC** — tidak ada koneksi masuk ke
PC, semua tetap outbound HTTPS dari sini.

## Verifikasi end-to-end

1. `.venv\Scripts\python -m pytest tests/ -v` → semua pass.
2. `curl -X POST https://dashboard.../api/integrations/itop -H 'X-API-Key: salah'` → 401.
3. Buat test UserRequest di iTop (org ter-mapping) → ≤60 detik muncul di dashboard
   (record SR, agent "iTop Sync Bot", status In Progres).
4. Set pending / resolve di iTop → status dashboard ikut berubah,
   resolve mengisi user response date/time.
5. Ulangi dengan Incident → record INC.
6. Stop container dashboard → agent retry & HWM diam; start lagi → data nyusul.

## Troubleshooting

| Gejala | Cek |
|---|---|
| `iTop error code=1` / unauthorized | Profile REST Services User belum ada di user `rest_sync` |
| 401 dari dashboard | `DASHBOARD_API_KEY` ≠ `ITOP_SYNC_API_KEY` |
| `client_code ... tidak ditemukan` | Kode di `clients_map.yaml` tidak cocok dengan `clients.code` dashboard |
| `org tidak ter-mapping` di log | Tambahkan org tsb ke `clients_map.yaml` |
| Belum ada HWM | Jalankan `sync.py --seed` dulu |
