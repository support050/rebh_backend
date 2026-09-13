import urllib.request
import json
import sys

# Set stdout encoding
sys.stdout.reconfigure(encoding='utf-8')

symbols = [
    ('2222', 'أرامكو', 'طاقة'),
    ('2010', 'سابك', 'مواد أساسية'),
    ('2280', 'المراعي', 'أغذية'),
    ('4003', 'إكسترا', 'تجزئة كمالية'),
    ('4190', 'جرير', 'تجزئة كمالية'),
    ('4001', 'أسواق العثيم', 'تجزئة أغذية'),
    ('2060', 'التصنيع', 'مواد أساسية'),
    ('2020', 'سابك للمغذيات', 'مواد أساسية'),
    ('1321', 'تهامة', 'خدمات استهلاكية'),
    ('2190', 'سيسكو القابضة', 'خدمات صناعية'),
    ('7010', 'stc', 'اتصالات'),
    ('2290', 'ينساب', 'مواد أساسية'),
    ('4007', 'سينومي ريتيل', 'تجزئة كمالية'),
    ('2082', 'أكوا باور', 'مرافق عامة'),
    ('1120', 'الراجحي', 'بنوك'),
    ('1150', 'الإنماء', 'بنوك'),
    ('1180', 'الأهلي', 'بنوك'),
    ('4330', 'الرياض ريت', 'صناديق ريت'),
    ('8010', 'التعاونية', 'تأمين'),
    ('8210', 'بوبا العربية', 'تأمين')
]

header = f"{'Symbol':<6} | {'Company':<16} | {'Sector':<16} | {'DSO':<7} | {'DIO':<7} | {'DPO':<7} | {'CCC':<7} | {'Notes'}"
print(header)
print('-' * len(header))

for sym, name, sec in symbols:
    try:
        url = f'http://localhost:8000/api/rebh/statements/{sym}'
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode('utf-8'))
        
        is_data = data.get('income_statement', {})
        bs_data = data.get('bs', {})
        
        rec = bs_data.get('receivables', [])
        inv = bs_data.get('inventory', [])
        pay = bs_data.get('payables', [])
        
        rec_val = rec[-1] if rec else 0
        inv_val = inv[-1] if inv else 0
        pay_val = pay[-1] if pay else 0
        
        rev = is_data.get('rev', [])
        cogs = is_data.get('cogs', [])
        
        rev_val = rev[-1] if rev else 0
        cogs_val = abs(cogs[-1]) if cogs else 0
        
        is_bank = 'بنوك' in sec or 'تأمين' in sec
        
        dso = round((rec_val / rev_val * 365), 1) if rev_val > 0 and rec_val > 0 else None
        dio = round((inv_val / cogs_val * 365), 1) if cogs_val > 0 and inv_val > 0 else None
        dpo = round((pay_val / cogs_val * 365), 1) if cogs_val > 0 and pay_val > 0 else None
        
        ccc = round(dso + dio - dpo, 1) if (dso is not None and dio is not None and dpo is not None) else None
        
        dso_s = f"{dso}d" if dso is not None else "-"
        dio_s = f"{dio}d" if dio is not None else "-"
        dpo_s = f"{dpo}d" if dpo is not None else "-"
        ccc_s = f"{ccc}d" if ccc is not None else "-"
        
        note = ''
        if is_bank:
            note = 'Bank/Insurance (Excluded)'
        elif dso is None and dio is None and dpo is None:
            note = 'No working capital items'
        elif dio is None:
            note = 'Service firm (No inventory)'
        else:
            note = 'Complete'
            
        print(f"{sym:<6} | {name:<16} | {sec:<16} | {dso_s:<7} | {dio_s:<7} | {dpo_s:<7} | {ccc_s:<7} | {note}")
    except Exception as e:
        print(f"{sym:<6} | {name:<16} | {sec:<16} | Error: {e}")
