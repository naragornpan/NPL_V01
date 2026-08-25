-- แก้ encoding ของ LED ให้ตรงกับหน้าเว็บจริง (UTF-8) — หยุด warning ตอนดึง
update sources set encoding = 'utf-8' where code = 'led_auction';
