"""
Keşfet sayfası için ABD hisseleri, sektörlere (neye hizmet ettiklerine) göre.
Fiyatlar Alpaca'nın ücretsiz anlık özetinden (snapshot, IEX) dakikada bir çekilir.
Listede olmayan ya da işlem görmeyen sembol sessizce atlanır.
"""

SEKTORLER = {
    "Yarı iletken": ["NVDA", "AMD", "AVGO", "TSM", "INTC", "MU", "QCOM", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ARM",
                     "ADI", "ON", "NXPI", "MCHP", "ASML", "SMCI"],
    "Yazılım ve bulut": ["MSFT", "ORCL", "CRM", "ADBE", "NOW", "PLTR", "SNOW", "CRWD", "PANW", "DDOG", "NET", "SHOP",
                         "INTU", "WDAY", "ZS", "MDB", "TEAM"],
    "İnternet ve iletişim": ["GOOGL", "META", "NFLX", "DIS", "SPOT", "PINS", "SNAP", "RDDT", "UBER", "ABNB", "DASH",
                             "T", "VZ", "TMUS", "CMCSA"],
    "Bilgisayar ve donanım": ["AAPL", "DELL", "HPQ", "HPE", "IBM", "CSCO", "ANET", "WDC", "STX"],
    "Havacılık ve savunma": ["BA", "LMT", "RTX", "NOC", "GD", "LHX", "TXT", "HII", "AXON", "KTOS", "RKLB", "JOBY", "ACHR"],
    "Havayolu ve seyahat": ["DAL", "UAL", "AAL", "LUV", "ALK", "CCL", "RCL", "NCLH", "BKNG", "EXPE", "MAR"],
    "Demir-çelik ve madencilik": ["NUE", "STLD", "CLF", "MT", "AA", "FCX", "SCCO", "NEM", "RIO", "BHP", "VALE", "MP", "CCJ"],
    "Sanayi ve makine": ["CAT", "DE", "GE", "GEV", "HON", "MMM", "ETN", "EMR", "PH", "ITW", "URI", "CMI", "PCAR"],
    "Petrol ve gaz": ["XOM", "CVX", "COP", "OXY", "SLB", "HAL", "EOG", "DVN", "MPC", "PSX", "VLO", "KMI"],
    "Temiz enerji ve nükleer": ["FSLR", "ENPH", "RUN", "PLUG", "OKLO", "SMR", "CEG", "VST"],
    "Banka ve finans": ["JPM", "BAC", "WFC", "C", "GS", "MS", "SCHW", "BLK", "AXP", "V", "MA", "PYPL", "COIN", "HOOD", "SOFI"],
    "Sağlık ve ilaç": ["LLY", "JNJ", "PFE", "MRK", "ABBV", "UNH", "AMGN", "GILD", "BMY", "MRNA", "ISRG", "TMO", "ABT",
                       "CVS", "NVO"],
    "Perakende ve tüketim": ["AMZN", "WMT", "COST", "HD", "LOW", "TGT", "NKE", "SBUX", "MCD", "KO", "PEP", "PG"],
    "Otomotiv ve elektrikli araç": ["TSLA", "F", "GM", "RIVN", "LCID", "TM", "STLA", "NIO", "LI", "XPEV"],
    "Kripto bağlantılı": ["MSTR", "MARA", "RIOT", "CLSK"],
    "Kamu hizmeti ve gayrimenkul": ["NEE", "DUK", "SO", "AMT", "PLD", "O", "SPG"],
    "Endeks ve emtia fonları": ["SPY", "QQQ", "IWM", "DIA", "TQQQ", "SQQQ", "SOXL", "XLF", "XLE", "TLT", "GLD", "SLV"],
}


def sektor_of(sym):
    for k, v in SEKTORLER.items():
        if sym in v:
            return k
    return None


def tum_semboller():
    seen = []
    for v in SEKTORLER.values():
        for s in v:
            if s not in seen:
                seen.append(s)
    return seen
