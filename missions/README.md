# Missions Directory

โฟลเดอร์นี้เก็บรายงานผลลัพธ์ของ Mission ทั้งหมด

## โครงสร้างไฟล์
- `mission_NNN_<ชื่อ>.md` -- รายงานสรุปผล (ภาษาไทย)
- `mission_NNN_<ชื่อ>.json` -- ข้อมูลดิบ
- State อยู่ที่ `research/missions.json`

## สั่งรัน
```bash
python research/missions.py --run          # รัน mission วันนี้
python research/missions.py --status       # ดูประวัติ
python research/missions.py --run --type factor_test  # บังคับประเภท
```
