# Standardized Template Codes and Mapping for Commercial Companies
# Covers Income Statement (IS), Balance Sheet (BS), and Cash Flow (CF)

import re
from difflib import SequenceMatcher

STANDARD_TEMPLATE = {
    'IS-010': {'statement': 'income_statement', 'line_en': 'Revenue / Turnover', 'line_ar': 'الإيرادات / المبيعات', 'is_subtotal': False},
    'IS-020': {'statement': 'income_statement', 'line_en': 'Cost of Sales', 'line_ar': 'تكلفة المبيعات', 'is_subtotal': False},
    'IS-030': {'statement': 'income_statement', 'line_en': 'Gross Profit', 'line_ar': 'إجمالي الربح', 'is_subtotal': True},
    'IS-040': {'statement': 'income_statement', 'line_en': 'Selling and Distribution Expenses', 'line_ar': 'مصاريف البيع والتوزيع', 'is_subtotal': False},
    'IS-050': {'statement': 'income_statement', 'line_en': 'General and Administrative Expenses', 'line_ar': 'المصاريف العمومية والإدارية', 'is_subtotal': False},
    'IS-060': {'statement': 'income_statement', 'line_en': 'Other Operating Income / Expenses', 'line_ar': 'إيرادات / مصاريف تشغيلية أخرى', 'is_subtotal': False},
    'IS-070': {'statement': 'income_statement', 'line_en': 'Operating Income (EBIT)', 'line_ar': 'الربح التشغيلي (الربح قبل الفوائد والضرائب)', 'is_subtotal': True},
    'IS-080': {'statement': 'income_statement', 'line_en': 'Finance Costs', 'line_ar': 'تكاليف التمويل', 'is_subtotal': False},
    'IS-090': {'statement': 'income_statement', 'line_en': 'Finance Income', 'line_ar': 'إيرادات التمويل', 'is_subtotal': False},
    'IS-100': {'statement': 'income_statement', 'line_en': 'Share of Profit of Associates & Joint Ventures', 'line_ar': 'حصة في أرباح شركات زميلة ومشاريع مشتركة', 'is_subtotal': False},
    'IS-110': {'statement': 'income_statement', 'line_en': 'Profit Before Zakat and Tax', 'line_ar': 'الربح قبل الزكاة والضريبة', 'is_subtotal': True},
    'IS-120': {'statement': 'income_statement', 'line_en': 'Zakat Expense', 'line_ar': 'مصروف الزكاة', 'is_subtotal': False},
    'IS-125': {'statement': 'income_statement', 'line_en': 'Income Tax Expense', 'line_ar': 'مصروف ضريبة الدخل', 'is_subtotal': False},
    'IS-130': {'statement': 'income_statement', 'line_en': 'Profit from Continuing Operations', 'line_ar': 'صافي الربح من العمليات المستمرة', 'is_subtotal': True},
    'IS-135': {'statement': 'income_statement', 'line_en': 'Profit from Discontinued Operations', 'line_ar': 'صافي الربح من العمليات غير المستمرة', 'is_subtotal': False},
    'IS-140': {'statement': 'income_statement', 'line_en': 'Net Profit for the Period', 'line_ar': 'صافي الربح للفترة', 'is_subtotal': True},
    'IS-150': {'statement': 'income_statement', 'line_en': 'Net Profit Attributable to Shareholders of Parent', 'line_ar': 'صافي الربح العائد لمساهمي الشركة الأم', 'is_subtotal': True},
    'IS-160': {'statement': 'income_statement', 'line_en': 'Basic Earnings per Share (EPS)', 'line_ar': 'ربحية السهم الأساسية', 'is_subtotal': False},
    'IS-170': {'statement': 'income_statement', 'line_en': 'Weighted Average Number of Shares', 'line_ar': 'المتوسط المرجح لعدد الأسهم', 'is_subtotal': False},
    'BS-010': {'statement': 'balance_sheet', 'line_en': 'Cash and Cash Equivalents', 'line_ar': 'النقد وما يماثله', 'is_subtotal': False},
    'BS-015': {'statement': 'balance_sheet', 'line_en': 'Short-term Investments', 'line_ar': 'استثمارات قصيرة الأجل', 'is_subtotal': False},
    'BS-020': {'statement': 'balance_sheet', 'line_en': 'Trade and Other Receivables', 'line_ar': 'الذمم المدينة وأرصدة مدينة أخرى', 'is_subtotal': False},
    'BS-030': {'statement': 'balance_sheet', 'line_en': 'Inventories', 'line_ar': 'المخزون', 'is_subtotal': False},
    'BS-035': {'statement': 'balance_sheet', 'line_en': 'Other Current Assets', 'line_ar': 'أصول متداولة أخرى', 'is_subtotal': False},
    'BS-040': {'statement': 'balance_sheet', 'line_en': 'Total Current Assets', 'line_ar': 'إجمالي الأصول المتداولة', 'is_subtotal': True},
    'BS-050': {'statement': 'balance_sheet', 'line_en': 'Property, Plant and Equipment (PPE)', 'line_ar': 'العقارات والآلات والمعدات', 'is_subtotal': False},
    'BS-060': {'statement': 'balance_sheet', 'line_en': 'Investment Properties', 'line_ar': 'العقارات الاستثمارية', 'is_subtotal': False},
    'BS-070': {'statement': 'balance_sheet', 'line_en': 'Intangible Assets and Goodwill', 'line_ar': 'الأصول غير الملموسة والشهرة', 'is_subtotal': False},
    'BS-075': {'statement': 'balance_sheet', 'line_en': 'Investments in Associates & Joint Ventures', 'line_ar': 'الاستثمارات في شركات زميلة ومشاريع مشتركة', 'is_subtotal': False},
    'BS-080': {'statement': 'balance_sheet', 'line_en': 'Other Non-Current Assets', 'line_ar': 'أصول غير متداولة أخرى', 'is_subtotal': False},
    'BS-090': {'statement': 'balance_sheet', 'line_en': 'Total Assets', 'line_ar': 'إجمالي الأصول', 'is_subtotal': True},
    'BS-100': {'statement': 'balance_sheet', 'line_en': 'Trade and Other Payables', 'line_ar': 'الذمم الدائنة وأرصدة دائنة أخرى', 'is_subtotal': False},
    'BS-110': {'statement': 'balance_sheet', 'line_en': 'Short-term Borrowings & Debt', 'line_ar': 'قروض وتسهيلات قصيرة الأجل', 'is_subtotal': False},
    'BS-115': {'statement': 'balance_sheet', 'line_en': 'Current Portion of Long-term Debt', 'line_ar': 'الجزء المتداول من القروض طويلة الأجل', 'is_subtotal': False},
    'BS-120': {'statement': 'balance_sheet', 'line_en': 'Zakat and Tax Liabilities', 'line_ar': 'مخصص الزكاة والضريبة', 'is_subtotal': False},
    'BS-125': {'statement': 'balance_sheet', 'line_en': 'Other Current Liabilities', 'line_ar': 'مطلوبات متداولة أخرى', 'is_subtotal': False},
    'BS-130': {'statement': 'balance_sheet', 'line_en': 'Total Current Liabilities', 'line_ar': 'إجمالي المطلوبات المتداولة', 'is_subtotal': True},
    'BS-140': {'statement': 'balance_sheet', 'line_en': 'Long-term Borrowings & Debt', 'line_ar': 'قروض وتسهيلات طويلة الأجل', 'is_subtotal': False},
    'BS-150': {'statement': 'balance_sheet', 'line_en': 'Employees End of Service Benefits', 'line_ar': 'مخصص مستحقات نهاية الخدمة للموظفين', 'is_subtotal': False},
    'BS-155': {'statement': 'balance_sheet', 'line_en': 'Other Non-Current Liabilities', 'line_ar': 'مطلوبات غير متداولة أخرى', 'is_subtotal': False},
    'BS-160': {'statement': 'balance_sheet', 'line_en': 'Total Liabilities', 'line_ar': 'إجمالي المطلوبات', 'is_subtotal': True},
    'BS-170': {'statement': 'balance_sheet', 'line_en': 'Share Capital', 'line_ar': 'رأس المال', 'is_subtotal': False},
    'BS-175': {'statement': 'balance_sheet', 'line_en': 'Statutory Reserve', 'line_ar': 'الاحتياطي النظامي', 'is_subtotal': False},
    'BS-180': {'statement': 'balance_sheet', 'line_en': 'Retained Earnings (Accumulated Losses)', 'line_ar': 'الأرباح المبقاة (الخسائر المتراكمة)', 'is_subtotal': False},
    'BS-182': {'statement': 'balance_sheet', 'line_en': 'Other Reserves', 'line_ar': 'احتياطيات أخرى', 'is_subtotal': False},
    'BS-185': {'statement': 'balance_sheet', 'line_en': 'Total Equity Attributable to Shareholders', 'line_ar': 'إجمالي حقوق الملكية الخاصة بمساهمي الشركة الأم', 'is_subtotal': True},
    'BS-188': {'statement': 'balance_sheet', 'line_en': 'Non-Controlling Interests', 'line_ar': 'الحصص غير المسيطرة', 'is_subtotal': False},
    'BS-190': {'statement': 'balance_sheet', 'line_en': 'Total Equity', 'line_ar': 'إجمالي حقوق الملكية', 'is_subtotal': True},
    'BS-200': {'statement': 'balance_sheet', 'line_en': 'Total Liabilities and Equity', 'line_ar': 'إجمالي المطلوبات وحقوق الملكية', 'is_subtotal': True},
    'CF-010': {'statement': 'cash_flow', 'line_en': 'Net Profit Before Zakat and Tax', 'line_ar': 'صافي الربح قبل الزكاة والضريبة', 'is_subtotal': False},
    'CF-020': {'statement': 'cash_flow', 'line_en': 'Depreciation and Amortization', 'line_ar': 'الاستهلاك والإطفاء', 'is_subtotal': False},
    'CF-030': {'statement': 'cash_flow', 'line_en': 'Other Non-Cash Adjustments', 'line_ar': 'تعديلات غير نقدية أخرى', 'is_subtotal': False},
    'CF-040': {'statement': 'cash_flow', 'line_en': 'Changes in Working Capital', 'line_ar': 'التغير في رأس المال العامل', 'is_subtotal': False},
    'CF-050': {'statement': 'cash_flow', 'line_en': 'Zakat and Tax Paid', 'line_ar': 'الزكاة والضريبة المدفوعة', 'is_subtotal': False},
    'CF-060': {'statement': 'cash_flow', 'line_en': 'Net Cash from Operating Activities (CFO)', 'line_ar': 'صافي النقد من الأنشطة التشغيلية', 'is_subtotal': True},
    'CF-070': {'statement': 'cash_flow', 'line_en': 'Capital Expenditures (CapEx)', 'line_ar': 'النفقات الرأسمالية (شراء عقارات ومعدات)', 'is_subtotal': False},
    'CF-080': {'statement': 'cash_flow', 'line_en': 'Other Investing Activities', 'line_ar': 'أنشطة استثمارية أخرى', 'is_subtotal': False},
    'CF-090': {'statement': 'cash_flow', 'line_en': 'Net Cash Used in Investing Activities (CFI)', 'line_ar': 'صافي النقد من الأنشطة الاستثمارية', 'is_subtotal': True},
    'CF-100': {'statement': 'cash_flow', 'line_en': 'Dividends Paid', 'line_ar': 'توزيعات الأرباح المدفوعة', 'is_subtotal': False},
    'CF-110': {'statement': 'cash_flow', 'line_en': 'Net Proceeds (Repayments) of Borrowings', 'line_ar': 'صافي متحصلات (سداد) قروض', 'is_subtotal': False},
    'CF-120': {'statement': 'cash_flow', 'line_en': 'Other Financing Activities', 'line_ar': 'أنشطة تمويلية أخرى', 'is_subtotal': False},
    'CF-130': {'statement': 'cash_flow', 'line_en': 'Net Cash from Financing Activities (CFF)', 'line_ar': 'صافي النقد من الأنشطة التمويلية', 'is_subtotal': True},
    'CF-140': {'statement': 'cash_flow', 'line_en': 'Net Change in Cash and Cash Equivalents', 'line_ar': 'صافي التغير في النقد وما يماثله', 'is_subtotal': True},
    'CF-150': {'statement': 'cash_flow', 'line_en': 'Cash and Cash Equivalents at Beginning', 'line_ar': 'النقد وما يماثله في بداية الفترة', 'is_subtotal': False},
    'CF-160': {'statement': 'cash_flow', 'line_en': 'Cash and Cash Equivalents at End', 'line_ar': 'النقد وما يماثله في نهاية الفترة', 'is_subtotal': True},
}

PARAM_MAPPING = {
    'accounts payable': ('BS-100', 1),
    'accounts receivable': ('BS-020', 1),
    'accrued expenses': ('BS-125', 1),
    'accrued income': ('BS-035', 1),
    'accumulated losses': ('BS-180', 1),
    'addition in cash and cash equivalents due to acquisition and establishment of subsidiaries': ('BS-010', 1),
    'additions to property and equipment': ('CF-070', -1),
    'adjustment for inventory written-off': ('CF-030', 1),
    'adjustment for provision for slow moving items and inventory shortage': ('CF-030', 1),
    'adjustment for provision of employees\' terminal benefit': ('CF-030', 1),
    'adjustment for provision, pension and government grants, net movements': ('CF-030', 1),
    'adjustments for amortization and impairment (reversal of impairment) of intangible assets': ('CF-020', 1),
    'adjustments for decrease (increase) in accrued income': ('CF-040', 1),
    'adjustments for decrease (increase) in due from related parties': ('CF-040', 1),
    'adjustments for decrease (increase) in inventories': ('CF-040', 1),
    'adjustments for decrease (increase) in other current assets': ('CF-040', 1),
    'adjustments for decrease (increase) in other receivables': ('CF-040', 1),
    'adjustments for decrease (increase) in prepayment': ('CF-040', 1),
    'adjustments for decrease (increase) in trade accounts receivable, net': ('CF-040', 1),
    'adjustments for deferred revenues': ('CF-030', 1),
    'adjustments for depreciation and impairment (reversal of impairment) of property, plant and equipments': ('CF-020', 1),
    'adjustments for fair value adjustment of a contingent consideration': ('CF-030', 1),
    'adjustments for finance costs': ('CF-030', 1),
    'adjustments for finance income': ('CF-030', -1),
    'adjustments for gain (loss) on disposal of property, plant and equipment': ('CF-030', 1),
    'adjustments for gains on disposal of investment property': ('CF-030', -1),
    'adjustments for impairment loss (reversal of impairment loss) recognized in statement of income': ('CF-030', 1),
    'adjustments for increase (decrease) in accrued expenses': ('CF-040', 1),
    'adjustments for increase (decrease) in advances from customers': ('CF-040', 1),
    'adjustments for increase (decrease) in due to related parties': ('CF-040', 1),
    'adjustments for increase (decrease) in other accounts payable': ('CF-040', 1),
    'adjustments for increase (decrease) in provisions': ('CF-040', 1),
    'adjustments for increase (decrease) in trade accounts payable': ('CF-040', 1),
    'adjustments for other current liabilities': ('CF-040', 1),
    'adjustments for other non-cash expenses': ('CF-030', 1),
    'adjustments for other non-cash income': ('CF-030', -1),
    'adjustments for other non-cash items': ('CF-030', 1),
    'adjustments for share of profit of an associate and joint venture': ('CF-030', -1),
    'adjustments for share-based payments expense': ('CF-030', 1),
    'adjustments for unrealised gain on derivative financial instruments': ('CF-030', 1),
    'adjustments for valuation gains on investment property': ('CF-030', -1),
    'administrative expenses': ('IS-050', -1),
    'advances from customers': ('BS-125', 1),
    'assets subject to finance lease': ('BS-050', 1),
    'bank balances and cash': ('BS-010', 1),
    'bank overdraft': ('BS-110', 1),
    # IS-160: prefer "total basic" (and plain basic). Component/diluted lines are blocklisted.
    'basic earnings per share': ('IS-160', 1),
    'board of directors\' remunerations': ('IS-060', -1),
    'business acquisition, net of cash and cash equivalents': ('BS-010', 1),
    'capital expenditures': ('CF-070', -1),
    'capitalisation of retained earnings and statutory reserve': ('BS-180', 1),
    'cash advances and loans made to other parties, classified as investing activities': ('CF-080', -1),
    'cash and balances with banks': ('BS-010', 1),
    'cash and balances with central banks': ('BS-010', 1),
    'cash and balances with saudi arabian monetary authority': ('BS-010', 1),
    'cash and bank balances': ('BS-010', 1),
    'cash and cash equivalents': ('BS-010', 1),
    'cash and cash equivalents at begining of period': ('CF-150', 1),
    'cash and cash equivalents at beginning of period': ('CF-150', 1),
    'cash and cash equivalents at beginning of the period': ('CF-150', 1),
    'cash and cash equivalents at end of period': ('CF-160', 1),
    'cash and cash equivalents at end of the period': ('CF-160', 1),
    'cash and cash equivalents at the beginning of the period': ('CF-150', 1),
    'cash and cash equivalents at the beginning of the period attributable to discounted operations': ('CF-150', 1),
    'cash and cash equivalents at the beginning of the year': ('CF-150', 1),
    'cash and cash equivalents at the end of the period': ('CF-160', 1),
    'cash and cash equivalents at the end of the year': ('CF-160', 1),
    'cash and cash equivalents disposed off related to a subsidiary': ('BS-010', 1),
    'cash and cash equivalents in relation to assets classified as held for sale': ('BS-010', 1),
    'cash and cash equivalents of disposal group classified': ('BS-010', 1),
    'cash and cash equivalents of disposal group classified as held for sale': ('BS-010', 1),
    'cash and cash equivalents related to unconsolidated subsidiary': ('BS-010', 1),
    'cash and cash equivalents through business combination': ('BS-010', 1),
    'cash and cash equivalents, insurance operations assets': ('BS-010', 1),
    'cash and cash equivalents, insurance operations\' assets at beginning of period': ('BS-010', 1),
    'cash and cash equivalents, insurance operations\' assets at end of period': ('BS-010', 1),
    'cash and cash equivalents, insurance/ takaful operations assets': ('BS-010', 1),
    'cash and cash equivalents, shareholders assets': ('BS-010', 1),
    'cash and cash equivalents, shareholders\' assets': ('BS-010', 1),
    'cash and cash equivalents.2': ('BS-010', 1),
    'cash at banks and on hand': ('BS-010', 1),
    'cash flows from investing activities': ('CF-090', 1),
    'cost of goods sold': ('IS-020', -1),
    'cost of raising share capital': ('CF-120', -1),
    'cost of revenue': ('IS-020', -1),
    'cost of revenues': ('IS-020', -1),
    'cost of sales': ('IS-020', -1),
    'current portion of long term loans': ('BS-115', 1),
    'current portion of long-term debt': ('BS-115', 1),
    'current portion of long-term loans': ('BS-115', 1),
    'customer deposits': ('BS-100', 1),
    'customer\'s deposits': ('BS-100', 1),
    'customers\' deposits': ('BS-100', 1),
    'debt securities, term loan, borrowings and sukuk in issue': ('BS-140', 1),
    'debt securities, term loans, borrowings and sukuks in issue': ('BS-140', 1),
    'decrease (increase) in operating assets': ('CF-040', 1),
    'deferred revenue, current': ('BS-125', 1),
    'deferred revenue, non-current': ('BS-155', 1),
    'deferred tax assets': ('BS-080', 1),
    'deferred tax liabilities': ('BS-155', 1),
    'deposits from customers': ('BS-100', 1),
    'depreciation and amortisation': ('CF-020', 1),
    'depreciation and amortisation expense': ('IS-050', -1),
    'depreciation and amortization': ('CF-020', 1),
    'derivative financial instruments/ assets, current': ('BS-035', 1),
    'derivative financial instruments/ assets, non-current': ('BS-080', 1),
    'derivative financial instruments/ liabilities, current': ('BS-125', 1),
    'derivative financial instruments/ liabilities, non-current': ('BS-155', 1),
    'derivatives assets': ('BS-035', 1),
    'derivatives liabilities': ('BS-125', 1),
    'description of accounting policy for cost of revenue': ('IS-020', -1),
    'disclosure of cash and cash equivalents': ('BS-010', 1),
    'disclosure of cost of revenue': ('IS-020', -1),
    'disclousre of cash and cash equivalents': ('BS-010', 1),
    'disclousre of cost of revenuess': ('IS-020', -1),
    'distribution expenses': ('IS-040', -1),
    'dividend income': ('IS-090', 1),
    'dividends paid': ('CF-100', -1),
    'dividends paid (other than to non-controlling interest), classified as financing activities': ('CF-100', -1),
    # NCI dividends blocklisted — do not double CF-100
    'dividends payable': ('BS-125', 1),
    'dividends received, classified as investing activities': ('CF-080', 1),
    'dividends received, classified as operating activities': ('CF-080', 1),
    'due from banks and other financial institutions': ('BS-015', 1),
    'due from related parties': ('BS-035', 1),
    'due from related parties - non-current portion': ('BS-080', 1),
    'due from related parties -non-current portion': ('BS-080', 1),
    'due to banks and other financial institutions': ('BS-100', 1),
    'due to related parties': ('BS-125', 1),
    'effect of exchange rate changes on cash and cash equivalents': ('BS-010', 1),
    'effect of exchange rate changes on cash and cash equivalents, net': ('BS-010', 1),
    'employee end-of-service benefits': ('BS-150', 1),
    'employees end of service benefits': ('BS-150', 1),
    'employees\' terminal benefits': ('BS-150', 1),
    # BS-185 filled via "total equity attributable..." only
    'exchange income, net': ('IS-010', 1),
    'expenditure on other intangible assets': ('CF-070', -1),
    'fee and commission income': ('IS-010', 1),
    'fee and commission income (expense)': ('IS-060', 1),
    'finance cost': ('IS-080', -1),
    'finance costs': ('IS-080', -1),
    'finance income': ('IS-090', 1),
    'finance income from investing activities': ('CF-080', 1),
    'finance lease, current': ('BS-125', 1),
    # finance leases, non-current → blocklisted (was doubling BS-155 with Other NCL)
    'financial assets, current': ('BS-015', 1),
    'financial assets, non-current': ('BS-060', 1),
    'financing charges': ('IS-080', -1),
    'financing costs': ('IS-080', -1),
    'financing income': ('IS-090', 1),
    'foreign exchange income (expense)': ('IS-060', 1),
    'gains (losses) on non-trading investments, net': ('IS-060', 1),
    'general and administration expenses': ('IS-050', -1),
    'general and administrative expenses': ('IS-050', -1),
    'general and administrative expenses, insurance operations': ('IS-050', -1),
    'general and administrative expenses, insurance/ takaful operations': ('IS-050', -1),
    'general and administrative expenses, shareholder\'s operations': ('IS-050', -1),
    'general and administrative expenses, shareholders operations': ('IS-050', -1),
    # general reserve → blocklisted (not Statutory Reserve BS-175)
    'goodwill': ('BS-070', 1),
    'government grants': ('BS-155', 1),
    'gross income': ('IS-030', 1),
    'gross premiums written': ('IS-010', 1),
    # --- IFRS 17 Insurance Standard Mapping ---
    'insurance revenue': ('IS-010', 1),
    'insurance service expense': ('IS-020', -1),
    'insurance service result': ('IS-030', 1),
    'net expenses from reinsurance contracts held': ('IS-060', -1),
    'insurance finance income (expenses)': ('IS-080', -1),
    'reinsurance finance income (expenses)': ('IS-090', 1),
    'insurance contract liabilities': ('BS-125', 1),
    'insurance contract assets': ('BS-035', 1),
    'reinsurance contract assets': ('BS-035', 1),
    'reinsurance contract liabilities': ('BS-125', 1),
    # --- REIT & Real Estate Standard Mapping ---
    'rental income': ('IS-010', 1),
    'property operating expenses': ('IS-020', -1),
    'management fees': ('IS-050', -1),
    'custody fees': ('IS-050', -1),
    'fund management fees': ('IS-050', -1),
    'gain (loss) on fair value of investment properties': ('IS-060', 1),
    'investment properties, at fair value': ('BS-060', 1),
    'investment properties, at cost': ('BS-060', 1),
    'net assets attributable to unitholders': ('BS-190', 1),
    'net asset value per unit': ('IS-160', 1),
    'gross profit': ('IS-030', 1),
    'gross profit (loss)': ('IS-030', 1),
    'impairment (reversal of impairment) charge for credit losses/ loans, financing and advances': ('IS-060', -1),
    'impairment (reversal of impairment) charge for investments, net': ('IS-060', -1),
    'impairment (reversal of impairment) charge for other financial assets': ('IS-060', -1),
    'impairment loss on financial assets': ('IS-060', -1),
    'impairment loss on trade receivables': ('IS-060', -1),
    'impairment losses on financial assets': ('IS-060', -1),
    'impairment of goodwill': ('IS-060', -1),
    'income from investments held at fair value through income statement': ('IS-010', 1),
    'income from operations': ('IS-070', 1),
    'income tax': ('IS-125', -1),
    'income tax expense': ('IS-125', -1),
    'income tax expenses': ('IS-125', -1),
    'income tax on continuing operations for period': ('IS-125', -1),
    'income tax payable': ('BS-120', 1),
    'income taxes and zakat': ('IS-120', -1),
    'income taxes paid (refund), classified as operating activities': ('CF-050', -1),
    'increase (decrease) in cash and cash equivalents before effect of exchange rate changes': ('BS-010', 1),
    'increase (decrease) in operating liabilities': ('CF-040', 1),
    'intangible assets': ('BS-070', 1),
    'intangible assets other than goodwill, net': ('BS-070', 1),
    'intangible assets, net': ('BS-070', 1),
    'interest expense': ('IS-080', -1),
    'interest income': ('IS-090', 1),
    'interest received, classified as investing activities': ('CF-080', 1),
    'interest received, classified as operating activities': ('CF-080', 1),
    'inventories': ('BS-030', 1),
    'inventories, net': ('BS-030', 1),
    'inventory': ('BS-030', 1),
    'inventory real estate properties': ('BS-060', 1),
    'investment in associates': ('BS-075', 1),
    'investment in joint ventures': ('BS-075', 1),
    'investment income': ('IS-090', 1),
    'investment properties': ('BS-060', 1),
    'investment property': ('BS-060', 1),
    'investments in associates and joint ventures': ('BS-075', 1),
    'investments in joint ventures and associates': ('BS-075', 1),
    'investments income': ('IS-090', 1),
    'investments, net': ('BS-060', 1),
    'islamic financing, current': ('BS-110', 1),
    'islamic financing, non-current': ('BS-140', 1),
    'ijara financing': ('BS-140', 1),
    'lease receivable, non current': ('BS-080', 1),
    'loans, financing and advances, net': ('BS-020', 1),
    'loans,financing and advances, net': ('BS-020', 1),
    'long term accounts payable': ('BS-140', 1),
    'long-term borrowings': ('BS-140', 1),
    'long-term debt': ('BS-140', 1),
    'marketing expenses': ('IS-040', -1),
    'murabaha and sukuk': ('BS-140', 1),
    'murabaha financing, current': ('BS-110', 1),
    'murabaha financing, non-current': ('BS-140', 1),
    'murabahas, current': ('BS-110', 1),
    'murabahas, non-current': ('BS-140', 1),
    'net amount transferred to retained earnings on disposal of equity investments held at fair value through other comprehensive income': ('BS-180', 1),
    'net cash flows from (used in) financing activities': ('CF-130', 1),
    'net cash flows from (used in) financing activities, insurance operations': ('CF-130', 1),
    'net cash flows from (used in) financing activities, insurance/ takaful operations': ('CF-130', 1),
    'net cash flows from (used in) investing activities': ('CF-090', 1),
    'net cash flows from (used in) investing activities, insurance operations': ('CF-090', 1),
    'net cash flows from (used in) investing activities, insurance/ takaful operations': ('CF-090', 1),
    'net cash flows from (used in) operating activities': ('CF-060', 1),
    'net cash flows from (used in) operating activities, insurance operations': ('CF-060', 1),
    'net cash flows from (used in) operating activities, insurance/ takaful operations': ('CF-060', 1),
    'net cash flows from financing activities': ('CF-130', 1),
    'net cash flows from investing activities': ('CF-090', 1),
    'net cash flows from operating activities': ('CF-060', 1),
    'net cash from financing activities': ('CF-130', 1),
    'net cash from investing activities': ('CF-090', 1),
    'net cash from operating activities': ('CF-060', 1),
    'net cash from operating activities before changes in working capital': ('CF-030', 1),
    'net change in cash and cash equivalents': ('CF-140', 1),
    'net change in working capital': ('CF-040', 1),
    'net financing and investment income': ('IS-010', 1),
    'net income': ('IS-140', 1),
    'net income (loss)': ('IS-140', 1),
    'net increase (decrease) due to working capital changes': ('CF-040', 1),
    'net increase (decrease) in cash and cash equivalents': ('CF-140', 1),
    'net increase (decrease) in cash and cash equivalents, insurance operations cash flow': ('CF-140', 1),
    'net interest income': ('IS-010', 1),
    'net premiums earned': ('IS-010', 1),
    'net profit': ('IS-140', 1),
    'net profit (loss)': ('IS-140', 1),
    'net profit (loss) attributable to shareholders of parent': ('IS-150', 1),
    'net revenue': ('IS-010', 1),
    'net sales': ('IS-010', 1),
    'net special commission income is calculated by applying the discount rate to the net defined benefit liability. the company recognises the following changes in the net defined benefit obligation in the statement of income under general and administrative expenses:': ('IS-050', -1),
    'non-controlling interest': ('BS-188', 1),
    'non-controlling interests': ('BS-188', 1),
    'operating cash flow before working capital changes': ('CF-030', 1),
    'operating income': ('IS-070', 1),
    'operating profit': ('IS-070', 1),
    'operating profit (loss)': ('IS-070', 1),
    'operating revenue': ('IS-010', 1),
    'other accounts payables': ('BS-100', 1),
    'other adjustments for working capital changes': ('CF-040', 1),
    'other adjustments to reconcile profit (loss) before tax to net cash flows': ('CF-030', 1),
    'other assets': ('BS-035', 1),
    'other current assets': ('BS-035', 1),
    'other current liabilities': ('BS-125', 1),
    'other expenses': ('IS-060', -1),
    'other general and administrative expenses': ('IS-050', -1),
    'other income': ('IS-060', 1),
    'other income (expenses), net': ('IS-060', 1),
    'other income from non operating activities, net': ('IS-060', 1),
    'other income, net': ('IS-060', 1),
    'other inflows (outflows) of cash, classified as financing activities': ('CF-120', 1),
    'other inflows (outflows) of cash, classified as investing activities': ('CF-080', 1),
    'other investment income': ('IS-090', 1),
    'other liabilities': ('BS-125', 1),
    'other non-current assets': ('BS-080', 1),
    'other non-current liabilities': ('BS-155', 1),
    'other operating expense': ('IS-060', -1),
    'other operating expenses': ('IS-060', -1),
    'other operating income': ('IS-060', 1),
    'other operating income (expenses), net': ('IS-060', 1),
    'other real estate, net': ('BS-035', 1),
    'other receivables': ('BS-035', 1),
    'other reserves': ('BS-182', 1),
    'payment for acquisition of associates and joint ventures': ('CF-080', -1),
    'payments of other equity instruments': ('CF-120', -1),
    'payments to acquire or redeem treasury shares': ('CF-120', -1),
    'prepayments': ('BS-035', 1),
    'prepayments and other receivables': ('BS-035', 1),
    'proceed from sales of property, plant and equipment': ('CF-080', 1),
    'proceeds from debt securities, term loans, borrowings, sukuks and murabahas': ('CF-110', 1),
    'proceeds from disposal of associates and joint ventures': ('CF-080', 1),
    'proceeds from disposal of investment properties': ('CF-080', 1),
    'proceeds from issuing other equity instruments': ('CF-120', 1),
    'proceeds from issuing shares': ('CF-120', 1),
    'proceeds from long-term borrowings': ('CF-110', 1),
    'proceeds from sale of financial assets': ('CF-080', 1),
    'proceeds from sale of property and equipment': ('CF-080', 1),
    'profit (loss) attributable to owners of parent': ('IS-150', 1),
    'profit (loss) before zakat and tax': ('IS-110', 1),
    'profit (loss) before zakat and income tax': ('IS-110', 1),
    'profit (loss) before zakat and tax from continuing operations': ('IS-110', 1),
    'profit (loss) before zakat and income tax from continuing operations': ('IS-110', 1),
    'profit before zakat and tax': ('IS-110', 1),
    'profit before zakat and income tax': ('IS-110', 1),
    'profit (loss) for period': ('IS-140', 1),
    'profit (loss) for period from continuing operations': ('IS-130', 1),
    'profit (loss) for period from discontinued operations': ('IS-135', 1),
    'profit (loss) from continuing operations': ('IS-130', 1),
    'profit (loss) from discontinued operations': ('IS-135', 1),
    'profit (loss) from operations': ('IS-070', 1),
    'profit (loss) of discontinued operations': ('IS-135', 1),
    'profit (loss), attributable to equity holders of parent company': ('IS-150', 1),
    'profit before zakat': ('IS-110', 1),
    'profit for period': ('IS-140', 1),
    'profit for the period': ('IS-140', 1),
    'profit from operations': ('IS-070', 1),
    'property and equipment, net': ('BS-050', 1),
    'property, plant and equipment': ('BS-050', 1),
    'property, plant and equipment, net': ('BS-050', 1),
    'proposed dividend': ('BS-125', 1),
    'provision for employees\' end of service benefits': ('BS-150', 1),
    'provision for employees\' terminal benefits': ('BS-150', 1),
    'provision for expected credit losses': ('IS-060', -1),
    'provisions, current': ('BS-125', 1),
    'provisions, non-current': ('BS-155', 1),
    'purchase of financial assets': ('CF-080', -1),
    'purchase of investment properties': ('CF-080', -1),
    'purchase of property and equipment': ('CF-070', -1),
    'purchase of property, plant and equipment': ('CF-070', -1),
    'realized gain on disposal of equity instruments designated at fair value through other comprehensive income transferred to retained earnings': ('BS-180', 1),
    'realized gain ondisposal of equity instruments designated at fair value through other comprehensive income transferred to retained earnings': ('BS-180', 1),
    'rent and premises related expenses': ('IS-050', -1),
    'rent receivables': ('BS-035', 1),
    'repayment of debt securities, term loans, borrowings, sukuks and murabahas': ('CF-110', -1),
    'repayment of long-term borrowings': ('CF-110', -1),
    'repayments of finance lease liabilities': ('CF-110', -1),
    'retained earnings': ('BS-180', 1),
    'retained earnings (accumulated losses)': ('BS-180', 1),
    'retained earnings - appropriated': ('BS-180', 1),
    'retained earnings - unappropriated': ('BS-180', 1),
    'revenue': ('IS-010', 1),
    'revenue from contracts with customers': ('IS-010', 1),
    'revenues': ('IS-010', 1),
    'reversal of impairment loss on property, plant and equipment': ('IS-060', 1),
    'salaries and employee related expenses': ('IS-050', -1),
    'sales': ('IS-010', 1),
    'sales revenue': ('IS-010', 1),
    'selling and distribution expenses': ('IS-040', -1),
    'selling and marketing expenses': ('IS-040', -1),
    'selling and marketing expenses, insurance/ takaful operations': ('IS-040', -1),
    'selling expenses': ('IS-040', -1),
    'selling, distribution and marketing expenses': ('IS-040', -1),
    'severance fees cost of revenue': ('IS-020', -1),
    'share capital': ('BS-170', 1),
    'share of profit (loss) of associates': ('IS-100', 1),
    'share of profit (loss) of associates and joint ventures': ('IS-100', 1),
    'share of profit (loss) of joint ventures and associates': ('IS-100', 1),
    'share of profit of associates': ('IS-100', 1),
    'share of results of associates and joint ventures': ('IS-100', 1),
    'short term borrowings': ('BS-110', 1),
    'short-term borrowings': ('BS-110', 1),
    'short-term deposits': ('BS-015', 1),
    'short-term investments': ('BS-015', 1),
    'special commission expenses / return on deposits': ('IS-020', -1),
    'special commission income/ gross financing and investment income': ('IS-010', 1),
    'statutory reserve': ('BS-175', 1),
    'sukuk, current': ('BS-110', 1),
    'sukuk, non-current': ('BS-140', 1),
    'sukuks, current': ('BS-110', 1),
    'sukuks, non-current': ('BS-140', 1),
    'tawarruq, current': ('BS-110', 1),
    'tawarruq, non-current': ('BS-140', 1),
    'the general and administrative expenses': ('IS-050', -1),
    'the selling and marketing expenses': ('IS-040', -1),
    'time deposits': ('BS-015', 1),
    'total adjustments to reconcile profit (loss)': ('CF-030', 1),
    'total adjustments to reconcile profit (loss) before tax to net cash flows': ('CF-030', 1),
    'total assets': ('BS-090', 1),
    'total basic earnings (loss) per share': ('IS-160', 1),
    'total current assets': ('BS-040', 1),
    'total current liabilities': ('BS-130', 1),
    'total equity': ('BS-190', 1),
    # BS-185: keep a single preferred total (synonyms blocklisted)
    'total equity attributable to owners of parent': ('BS-185', 1),
    'total equity attributable to shareholders of parent': ('BS-185', 1),
    # Banks use "of bank" as their sole BS-185 caption (no "of parent" variant exists
    # in bank taxonomy) — confirmed via Saudi Investment Bank (1030) raw balance sheet,
    # which has ONLY this label and no other. Previously blocklisted, which silently
    # zeroed out BS-185 for every bank. Restored here.
    'total equity attributable to equity holders of bank': ('BS-185', 1),
    'total liabilities': ('BS-160', 1),
    'total liabilities and equity': ('BS-200', 1),
    'total operating revenue': ('IS-010', 1),
    'total other reserves': ('BS-182', 1),
    'total revenue': ('IS-010', 1),
    'total shareholders liabilities and equity': ('BS-200', 1),
    'trade accounts payables': ('BS-100', 1),
    'trade accounts receivable': ('BS-020', 1),
    'trade and other payables': ('BS-100', 1),
    'trade and other receivables': ('BS-020', 1),
    'trade payables': ('BS-100', 1),
    'trade receivables': ('BS-020', 1),
    'trade receivables, net': ('BS-020', 1),
    'trading income (expense)': ('IS-060', 1),
    'trading income, net': ('IS-010', 1),
    'transfer from proposed reserve to retained earnings': ('BS-180', 1),
    'transfer from retained earnings': ('BS-180', 1),
    'transfer from statutory reserve to retained earnings': ('BS-180', 1),
    'transfer from voluntary reserve to reserve retained earnings': ('BS-180', 1),
    'transfer of gains from the exclusion of equity investments at fair value through other comprehensive income to retained earnings': ('BS-180', 1),
    'transfer of realised gains from finanical assets held at fvtoci to retained earnings': ('BS-180', 1),
    'transfer to retained earnings': ('BS-180', 1),
    'transferred to retained earnings': ('BS-180', 1),
    'transfers to retained earnings on disposal of fvoci equity investments': ('BS-180', 1),
    'weighted average number of equity shares outstanding': ('IS-170', 1),
    'weighted average number of ordinary shares outstanding': ('IS-170', 1),
    'weighted average number of shares': ('IS-170', 1),
    'zakat': ('IS-120', -1),
    'zakat and income tax': ('IS-120', -1),
    'zakat and income tax expenses': ('IS-120', -1),
    'zakat and income tax liabilities': ('BS-120', 1),
    'zakat and income tax paid': ('CF-050', -1),
    'zakat expense': ('IS-120', -1),
    'zakat expenses': ('IS-120', -1),
    'zakat expenses on continuing operations for period': ('IS-120', -1),
    'zakat paid': ('CF-050', -1),
    'zakat paid, classified as operating activities': ('CF-050', -1),
    'zakat payable': ('BS-120', 1),
}

# Labels that must NOT map to any std_code (prevents synonym double-counting via fuzzy match).
# They remain visible under Raw / Other-Unmapped.
MAPPING_BLOCKLIST = {
    # EPS: keep only total basic / plain basic in PARAM_MAPPING
    'basic earnings (loss) per share from continuing operations',
    'basic earnings (loss) per share from discontinued operations',
    'diluted earnings (loss) per share from continuing operations',
    'diluted earnings (loss) per share from discontinued operations',
    'diluted earnings per share',
    'earnings per share',
    'total diluted earnings (loss) per share',
    # CF-010 synonyms (prefer continuing-operations line)
    'profit (loss) before zakat and income tax',
    'profit (loss) before zakat and income tax from discontinued operations',
    'profit (loss) for period before zakat and income tax',
    'profit before zakat and income tax',
    # CF-060 synonym / section header
    'cash flows from operating activities',
    'net cash flows from (used in) operations',
    # CF-050: not zakat/tax paid; often duplicates zakat line
    'interest paid, classified as operating activities',
    'other inflows (outflows) of cash, classified as operating activities',
    'total other inflows (outflows) of cash, classified as operating activities',
    # BS-182 components (prefer total other reserves / other reserves)
    'asset revaluation reserve',
    'available-for-sale reserve',
    'employee share based plan reserve',
    'miscellaneous other reserves',
    'other equity interest',
    'reserve of exchange differences on translation',
    'share premium',
    'treasury shares',
    # zakat discontinued
    'zakat expenses on discontinued operations for period',
    # BS-185 synonyms (prefer owners/shareholders of parent)
    # NOTE: 'total equity attributable to equity holders of bank' moved OUT of this
    # blocklist and into PARAM_MAPPING above — see comment there. It is the sole
    # BS-185 caption for banks, not a duplicate of the "of parent" phrasing.
    'equity attributable to owners of parent',
    'total equity attributable to equity holders of company',
    'total equity attributable to equity holders of parent company',
    # CF-100 NCI dividends
    'dividends paid to non-controlling interest, classified as financing activities',
    # BS-175 / BS-155 hygiene
    'general reserve',
    'finance leases, non-current',
    # Prevent non-current totals from mapping to total assets/liabilities
    'total non-current assets',
    'total non current assets',
    'total non-current liabilities',
    'total non current liabilities',
}

FUZZY_THRESHOLD = 0.92

_SWAP_PHRASES = (
    ("associates and joint ventures", "joint ventures and associates"),
    ("joint ventures and associates", "associates and joint ventures"),
)


def normalize_label(label: str) -> str:
    if not label:
        return ""
    s = str(label).replace("\xa0", " ").lower().strip()
    s = re.sub(r"\s*\[abstract\]", "", s, flags=re.I)
    s = re.sub(r"\s*\[line items\]", "", s, flags=re.I)
    s = re.sub(r"\s*\[member\]", "", s, flags=re.I)
    s = s.replace(",", " ")
    s = re.sub(r"\s*/\s*", " / ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _token_key(label: str) -> str:
    tokens = sorted(t for t in normalize_label(label).replace("/", " ").split() if t)
    return " ".join(tokens)


def _build_blocklist_keys():
    keys = set()
    for label in MAPPING_BLOCKLIST:
        raw = label.lower().strip()
        keys.add(raw)
        keys.add(normalize_label(label))
        keys.add(_token_key(label))
    return keys


_BLOCKLIST_KEYS = _build_blocklist_keys()


def _is_blocklisted(label: str) -> bool:
    raw = str(label).lower().strip()
    if raw in _BLOCKLIST_KEYS:
        return True
    norm = normalize_label(label)
    if norm in _BLOCKLIST_KEYS:
        return True
    tk = _token_key(label)
    return bool(tk and tk in _BLOCKLIST_KEYS)


def _build_lookup_indexes():
    norm_map = {}
    token_map = {}
    ambiguous = set()
    for key, mapping in PARAM_MAPPING.items():
        if _is_blocklisted(key):
            continue
        nk = normalize_label(key)
        if nk and nk not in norm_map:
            norm_map[nk] = mapping
        tk = _token_key(key)
        if not tk:
            continue
        if tk in token_map and token_map[tk][0] != mapping[0]:
            ambiguous.add(tk)
        elif tk not in token_map:
            token_map[tk] = mapping
    for tk in ambiguous:
        token_map.pop(tk, None)
    return norm_map, token_map


_NORM_MAP, _TOKEN_MAP = _build_lookup_indexes()


def resolve_mapping(label: str, statement: str = None):
    if not label:
        return None
    if _is_blocklisted(label):
        return None
    
    def _is_valid_for_stmt(m):
        if not m or not statement:
            return True
        code = m[0]
        expected_stmt = STANDARD_TEMPLATE.get(code, {}).get('statement')
        return expected_stmt is None or expected_stmt == statement

    raw = str(label).lower().strip()
    if raw in PARAM_MAPPING:
        m = PARAM_MAPPING[raw]
        if _is_valid_for_stmt(m):
            return m
    norm = normalize_label(label)
    if not norm:
        return None
    if norm in PARAM_MAPPING:
        m = PARAM_MAPPING[norm]
        if _is_valid_for_stmt(m):
            return m
    if norm in _NORM_MAP:
        m = _NORM_MAP[norm]
        if _is_valid_for_stmt(m):
            return m
    for a, b in _SWAP_PHRASES:
        if a in norm:
            swapped = norm.replace(a, b)
            if swapped in _NORM_MAP:
                m = _NORM_MAP[swapped]
                if _is_valid_for_stmt(m):
                    return m
            if swapped in PARAM_MAPPING:
                m = PARAM_MAPPING[swapped]
                if _is_valid_for_stmt(m):
                    return m
    tk = _token_key(label)
    if tk in _TOKEN_MAP:
        m = _TOKEN_MAP[tk]
        if _is_valid_for_stmt(m):
            return m
    best = None
    best_ratio = 0.0
    for nk, mapping in _NORM_MAP.items():
        if not _is_valid_for_stmt(mapping):
            continue
        if abs(len(nk) - len(norm)) > max(8, int(len(norm) * 0.25)):
            continue
        ratio = SequenceMatcher(None, norm, nk).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best = mapping
    if best is not None and best_ratio >= FUZZY_THRESHOLD:
        return best
    return None