import random, os, sys
random.seed(1)
S=sys.argv[1]; F="deals_fo_to_kondor"
os.makedirs(f"{S}/corpus/{F}/in",exist_ok=True); os.makedirs(f"{S}/corpus/{F}/out",exist_ok=True)
cp={"BANK_A":"1001","BANK_B":"1002","BANK_C":"1003","BANK_D":"1004","BANK_E":"1005"}
books={"FX-SPOT":"12","FX-FWD":"13","MM":"20"}
tid=0
for day in range(1,21):
    xml=['<?xml version="1.0"?><ns:Trades xmlns:ns="urn:fo">']; csvl=[]
    for _ in range(40):
        tid+=1; c=random.choice(list(cp)); b=random.choice(list(books))
        amt=round(random.uniform(1e4,5e6),2); side=random.choice(["BUY","SELL"])
        status=random.choices(["NEW","CANCEL"],[0.9,0.1])[0]; ccy=random.choice(["EUR","USD","GBP"])
        td=f"2026-09-{day:02d}"; vd=f"2026-10-{day:02d}"
        xml.append(f'<ns:Trade id="{tid:08d}"><ns:Status>{status}</ns:Status><ns:TradeDate>{td}T10:00:00Z</ns:TradeDate>'
                   f'<ns:ValueDate>{vd}</ns:ValueDate><ns:Notional ccy="{ccy.lower()}">{amt}</ns:Notional>'
                   f'<ns:Side>{side}</ns:Side><ns:Counterparty><ns:Code>{c}</ns:Code><ns:Name>Bank {c}</ns:Name></ns:Counterparty>'
                   f'<ns:Book>{b}</ns:Book></ns:Trade>')
        if status=="NEW":
            a = -amt if side=="SELL" else amt
            csvl.append(f"{tid};{td.replace('-','')};{vd.replace('-','')};{a:.2f};{ccy};{'B' if side=='BUY' else 'S'};{cp[c]};{books[b]};FO")
    xml.append("</ns:Trades>")
    open(f"{S}/corpus/{F}/in/TRADES_{day:02d}.xml","w").write("".join(xml))
    open(f"{S}/corpus/{F}/out/Deals_{day:02d}.csv","w").write("\n".join(csvl)+"\n")
