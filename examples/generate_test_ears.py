"""Génère un groupe d'EAR BW5 de test : FO_Trades.ear -> JMS -> Kondor_Deals.ear (+ projlib partagée)."""
import io, sys, zipfile, os

OUT = sys.argv[1]
os.makedirs(f"{OUT}/ears", exist_ok=True)
os.makedirs(f"{OUT}/deploy", exist_ok=True)

HDR = ('<?xml version="1.0" encoding="UTF-8"?>\n<pd:ProcessDefinition xmlns:pd="http://xmlns.tibco.com/bw/process/2003" '
       'xmlns:xsl="http://www.w3.org/1999/XSL/Transform" xmlns:tib="http://www.tibco.com/bw/xslt/custom-functions" '
       'xmlns:ns="http://www.tibco.com/pe/GenerateXSDFromGlobalVariables" xmlns:pfx="http://kondor/deals">')


def dd(name, gv):
    pairs = "".join(f"<NameValuePair><name>{k}</name><value>{v}</value></NameValuePair>" for k, v in gv.items())
    return (f'<?xml version="1.0"?><DeploymentDescriptors xmlns="http://www.tibco.com/xmlns/dd"><name>{name}</name>'
            f'<NameValuePairs><name>Global Variables</name>{pairs}</NameValuePairs></DeploymentDescriptors>').encode()


def zbytes(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for k, v in files.items():
            z.writestr(k, v)
    return b.getvalue()


READ_TRADES = HDR + """
<pd:name>Processes/ReadTrades.process</pd:name>
<pd:startName>File Poller</pd:startName>
<pd:starter name="File Poller">
  <pd:type>com.tibco.plugin.file.FileEventSource</pd:type>
  <config><pollInterval>5</pollInterval><fileName>%%Dirs/In%%/TRADES_*.xml</fileName></config>
</pd:starter>
<pd:activity name="Parse Trades">
  <pd:type>com.tibco.plugin.xml.XMLParseActivity</pd:type>
  <pd:inputBindings><xmlString><xsl:value-of select="$File-Poller/EventSourceOuputTextClass/fileContent/textContent"/></xmlString></pd:inputBindings>
</pd:activity>
<pd:group name="Each Trade">
  <pd:activity name="Send Trade">
    <pd:type>com.tibco.plugin.jms.JMSQueueSendActivity</pd:type>
    <config><SessionAttributes><destination>%%JMS/TradesQueue%%</destination></SessionAttributes>
      <ConnectionReference>/Shared/EMS.sharedjmscon</ConnectionReference></config>
    <pd:inputBindings><ActivityInput><Body><xsl:copy-of select="$current"/></Body></ActivityInput></pd:inputBindings>
  </pd:activity>
</pd:group>
<pd:transition><pd:from>File Poller</pd:from><pd:to>Parse Trades</pd:to><pd:conditionType>always</pd:conditionType></pd:transition>
<pd:transition><pd:from>Parse Trades</pd:from><pd:to>Send Trade</pd:to><pd:conditionType>xpath</pd:conditionType>
  <pd:xpath>$current/Status != 'CANCEL'</pd:xpath></pd:transition>
</pd:ProcessDefinition>"""

SEND_KONDOR = HDR + """
<pd:name>Processes/SendKondor.process</pd:name>
<pd:startName>JMS Queue Receiver</pd:startName>
<pd:starter name="JMS Queue Receiver">
  <pd:type>com.tibco.plugin.jms.JMSQueueEventSource</pd:type>
  <config><SessionAttributes><destination>%%JMS/Queue/Trades%%</destination></SessionAttributes></config>
</pd:starter>
<pd:activity name="Lookup Cpty">
  <pd:type>com.tibco.pe.core.CallProcessActivity</pd:type>
  <config><processName>/Common/Lookups/CptyLookup.process</processName></config>
  <pd:inputBindings><root><code><xsl:value-of select="$JMS-Queue-Receiver/ActivityOutput/Body/Trade/Counterparty/Code"/></code></root></pd:inputBindings>
</pd:activity>
<pd:activity name="Render Deals">
  <pd:type>com.tibco.plugin.parse.RenderActivity</pd:type>
  <pd:inputBindings><Input><pfx:Deal>
    <pfx:Deals_Id><xsl:value-of select="number($JMS-Queue-Receiver/ActivityOutput/Body/Trade/@id)"/></pfx:Deals_Id>
    <pfx:TradeDate><xsl:value-of select="tib:format-date('yyyyMMdd', substring($JMS-Queue-Receiver/ActivityOutput/Body/Trade/TradeDate, 1, 10))"/></pfx:TradeDate>
    <pfx:ValueDate><xsl:value-of select="translate($JMS-Queue-Receiver/ActivityOutput/Body/Trade/ValueDate, '-', '')"/></pfx:ValueDate>
    <pfx:Amount><xsl:choose>
      <xsl:when test="$JMS-Queue-Receiver/ActivityOutput/Body/Trade/Side = 'SELL'"><xsl:value-of select="-1 * $JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional"/></xsl:when>
      <xsl:otherwise><xsl:value-of select="$JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional"/></xsl:otherwise>
    </xsl:choose></pfx:Amount>
    <pfx:Currencies_Id><xsl:value-of select="upper-case($JMS-Queue-Receiver/ActivityOutput/Body/Trade/Notional/@ccy)"/></pfx:Currencies_Id>
    <pfx:BuySell><xsl:value-of select="substring($JMS-Queue-Receiver/ActivityOutput/Body/Trade/Side, 1, 1)"/></pfx:BuySell>
    <pfx:Cpty_Id><xsl:value-of select="$Lookup-Cpty/root/kondorId"/></pfx:Cpty_Id>
    <pfx:Folders_Id><xsl:value-of select="tib:lookup('BOOKS', $JMS-Queue-Receiver/ActivityOutput/Body/Trade/Book)"/></pfx:Folders_Id>
    <pfx:Source>FO</pfx:Source>
  </pfx:Deal></Input></pd:inputBindings>
</pd:activity>
<pd:activity name="Write Kondor File">
  <pd:type>com.tibco.plugin.file.FileWriteActivity</pd:type>
  <config><append>true</append></config>
  <pd:inputBindings><WriteActivityInputTextClass>
    <fileName><xsl:value-of select="concat($_globalVariables/ns:GlobalVariables/Dirs/KondorImport, '/Deals_', tib:format-dateTime('yyyyMMdd', current-dateTime()), '.csv')"/></fileName>
    <textContent><xsl:value-of select="$Render-Deals/string"/></textContent>
  </WriteActivityInputTextClass></pd:inputBindings>
</pd:activity>
<pd:transition><pd:from>JMS Queue Receiver</pd:from><pd:to>Lookup Cpty</pd:to><pd:conditionType>always</pd:conditionType></pd:transition>
<pd:transition><pd:from>Lookup Cpty</pd:from><pd:to>Render Deals</pd:to><pd:conditionType>always</pd:conditionType></pd:transition>
<pd:transition><pd:from>Render Deals</pd:from><pd:to>Write Kondor File</pd:to><pd:conditionType>always</pd:conditionType></pd:transition>
</pd:ProcessDefinition>"""

CPTY_LOOKUP = HDR + """
<pd:name>Common/Lookups/CptyLookup.process</pd:name>
<pd:startName>Start</pd:startName>
<pd:activity name="Query REF_CPTY">
  <pd:type>com.tibco.plugin.jdbc.JDBCQueryActivity</pd:type>
  <config><statement>select KONDOR_ID from REF_CPTY where CODE = ?</statement>
    <jdbcSharedConfig>/Shared/RefDB.sharedjdbc</jdbcSharedConfig></config>
</pd:activity>
<pd:transition><pd:from>Start</pd:from><pd:to>Query REF_CPTY</pd:to><pd:conditionType>always</pd:conditionType></pd:transition>
</pd:ProcessDefinition>"""

# EAR 1 : lecture front-office -> JMS (la file est surchargée au déploiement)
par1 = zbytes({"TIBCO.xml": dd("Process Archive", {}), "Processes/ReadTrades.process": READ_TRADES})
open(f"{OUT}/ears/FO_Trades.ear", "wb").write(zbytes({
    "TIBCO.xml": dd("FO_Trades", {"Dirs/In": "/data/bw/in", "JMS/TradesQueue": "queue.trades.default"}),
    "Process Archive.par": par1}))

# EAR 2 : JMS -> fichier d'import Kondor, avec une projlib partagée dans le .sar
projlib = zbytes({"Common/Lookups/CptyLookup.process": CPTY_LOOKUP})
sar = zbytes({"TIBCO.xml": dd("Shared Archive", {}), "Common.projlib": projlib,
              "Shared/EMS.sharedjmscon": "<jms/>"})
par2 = zbytes({"TIBCO.xml": dd("Process Archive", {}), "Processes/SendKondor.process": SEND_KONDOR})
open(f"{OUT}/ears/Kondor_Deals.ear", "wb").write(zbytes({
    "TIBCO.xml": dd("Kondor_Deals", {"JMS/Queue/Trades": "queue.fo.trades", "Dirs/KondorImport": "/opt/kplus/import"}),
    "Process Archive.par": par2, "Shared Archive.sar": sar}))

# Config de déploiement exportée (AppManage) : surcharge de la file JMS de l'EAR 1
open(f"{OUT}/deploy/FO_Trades.xml", "w").write(
    '<?xml version="1.0"?><application xmlns="http://www.tibco.com/xmlns/ApplicationManagement" name="FO_Trades">'
    '<NVPairs name="Global Variables"><NameValuePair><name>JMS/TradesQueue</name><value>queue.fo.trades</value>'
    '</NameValuePair></NVPairs></application>')
print("OK")
