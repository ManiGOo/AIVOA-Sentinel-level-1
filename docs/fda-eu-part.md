H**ere the public, official APIs we can use** instead of web scraping, though the availability and format differ significantly between the US and the EU. \[1, 2\]

Using APIs is highly recommended because your code will not break when the agencies update their website designs. \[3, 4, 5, 6\]

## ---

**1\. United States: The openFDA API**

The **US FDA** provides a fully open, incredibly robust REST API backend called **openFDA**. It requires no registration or keys for basic usage (though you can sign up for a free API key to increase rate limits). \[7, 8, 9, 10, 11\]

> *   
> * **The Endpoint:** https://fda.gov  
> * **How to Query It:** It is built on Elasticsearch, meaning you can filter directly by date, manufacturer, or molecule name right inside the URL.  
> * **Example Python Request:** \[7, 8\]  
> * 

import requests

*\# Fetch the 5 most recent drug recall/enforcement reports*  
url \= "https://fda.gov"  
data \= requests.get(url).json()

for record in data\['results'\]:  
    print(f"Firm: {record.get('recalling\_firm')}")  
    print(f"Reason: {record.get('reason\_for\_recall')}\\n")

## ---

**2\. European Union: The EMA Developer Portal**

The **European Medicines Agency (EMA)** has been modernising its systems and provides several modern public APIs via its developer platforms. \[12, 13\]

## **A. EMA Electronic Product Information (ePI) API \[12\]**

If you want to track changes, recalls, and updates to human medicines centrally authorized in the EU, the EMA offers a public, keyless API. \[12\]

> *   
> * **The Endpoint:** Accessible via the [**EMA ePI Developer Portal**](https://epi.developer.ema.europa.eu/api-details).  
> * **Data Format:** It delivers data structured in **HL7 FHIR JSON format** (Fast Healthcare Interoperability Resources), making it highly standardized. \[12, 14\]  
> * 

## **B. The Union Product Database (UPD) API**

For automated, machine-to-machine extraction of medicine data across the entire EU, the EMA operates a read-only public API. \[15\]

> *   
> * **What it tracks:** Systematic data on approved medicines, manufacturing variations, and regulatory adjustments.  
> * **Access Requirements:** Registration instructions can be found directly on the [EMA Veterinary and Public UPD Portal](https://ec.europa.eu/newsroom/ema/items/871850/en). \[15, 16, 17\]  
> * 

## **C. EudraGMDP (Manufacturing/Facility Failures)**

For factory-level quality failures (GMP non-compliance), the EMA hosts the [**EudraGMDP Registration Portal**](https://eudragmdp.ema.europa.eu/inspections/view/apiReg/APIRegistrationHome.xhtml). Unlike openFDA, you cannot immediately query this anonymously via a URL. You must apply for official read-only "API Client" credentials on their registration page to hook your code directly into their backend systems. \[18, 19\]

## ---

**💡 Recommendation for Your Architecture**

If you are building an automated surveillance tool:

> 1. **Use the Native REST API** for the **US FDA** (zero scraping needed).  
> 2. **Use the EMA ePI / UPD APIs** for **Centralised EU** data.  
> 3. **Keep localized web scrapers** (like the BeautifulSoup script provided earlier) *only* for specific EU country websites (like Ireland's HPRA or Germany's BfArM) that choose not to expose direct public endpoints. \[3, 7, 12, 15\]

Would you like help rewriting the previous script to **completely utilize the openFDA and EMA API endpoints** instead of parsing HTML?

\[1\] [https://www.okta.com](https://www.okta.com/blog/industry-insights/api-application-programming-interface/)  
\[2\] [https://oxylabs.io](https://oxylabs.io/blog/api-vs-web-scraping)  
\[3\] [https://scrapegraphai.com](https://scrapegraphai.com/blog/clinical-data-extraction)  
\[4\] [https://docs.developer.tech.gov.sg](https://docs.developer.tech.gov.sg/docs/complete-apex-user-guide/sections/publishing/update-api)  
\[5\] [https://www.kern-it.be](https://www.kern-it.be/en/definitions/web-scraping/)  
\[6\] [https://medium.com](https://medium.com/@trading.dude/web-scraping-vs-official-apis-for-financial-data-stocks-crypto-and-forex-89efb2a1818f)  
\[7\] [https://open.fda.gov](https://open.fda.gov/apis/)  
\[8\] [https://dlthub.com](https://dlthub.com/context/source/fda-data)  
\[9\] [https://www.accessdata.fda.gov](https://www.accessdata.fda.gov/scripts/feiportal/apidocs/)  
\[10\] [https://www.decipherzone.com](https://www.decipherzone.com/blog-detail/types-of-apis)  
\[11\] [https://developer.ons.gov.uk](https://developer.ons.gov.uk/)  
\[12\] [https://epi.developer.ema.europa.eu](https://epi.developer.ema.europa.eu/api-details)  
\[13\] [https://www.ema.europa.eu](https://www.ema.europa.eu/en/events/product-management-service-pms-public-api-beta-release-technical-overview-live-demo)  
\[14\] [https://arkenea.com](https://arkenea.com/blog/hipaa-api/)  
\[15\] [https://ec.europa.eu](https://ec.europa.eu/newsroom/ema/items/871850/en)  
\[16\] [https://www.lexology.com](https://www.lexology.com/api-integrations)  
\[17\] [https://www.idspay.in](https://www.idspay.in/pan-to-udyam-api)  
\[18\] [https://eudragmdp.ema.europa.eu](https://eudragmdp.ema.europa.eu/inspections/view/apiReg/APIRegistrationHome.xhtml)  
\[19\] [https://eudragmdp.ema.europa.eu](https://eudragmdp.ema.europa.eu/help_public/content/eudragmp/login/access_system.htm)