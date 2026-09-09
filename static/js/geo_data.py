// Geo data: Country → Cities → Districts
// Focused on GCC + major business countries
const GEO = {
  "Saudi Arabia": {
    ar: "المملكة العربية السعودية",
    cities: {
      "Riyadh":       { ar: "الرياض",      districts: ["Al Olaya","Al Malaz","Al Murabba","Al Naseem","Al Rawdah","Al Sulaimaniyah","Al Aqiq","Al Maseef","Al Wurud","Al Rabwah","Diplomatic Quarter","Al Nakheel","Al Sahafah","Al Rabi","Al Qirawan","Al Yasmin","Al Narjis","Al Arid","Al Munsiyah","Al Khaleej"] },
      "Jeddah":       { ar: "جدة",          districts: ["Al Balad","Al Hamra","Al Rawdah","Al Andalus","Al Zahraa","Al Shati","Al Naeem","Al Faisaliyah","Al Aziziyah","Obhur Al Shamaliyah","Al Marwah","Al Safa","Al Rehab","Al Khalidiyah","Al Nuzlah Al Yamaniyah"] },
      "Mecca":        { ar: "مكة المكرمة",  districts: ["Al Adl","Al Aziziyah","Al Shasha","Al Hujoun","Ajyad","Al Massfalah","Al Nakkasah","Al Rusaifah","Al Zahir","Kudai"] },
      "Medina":       { ar: "المدينة المنورة", districts: ["Al Haram","Al Aziziyah","Al Khalidiyah","Al Iskan","Quba","Al Rawdah","Al Manakhah","Al Buda","Al Difa"] },
      "Dammam":       { ar: "الدمام",       districts: ["Al Faisaliyah","Al Noor","Al Badiyah","Al Shulah","Al Jalawiyah","Al Hamra","Al Aziziyah","Al Rehab","Al Khobar Road","Al Wahah"] },
      "Al Khobar":    { ar: "الخبر",        districts: ["Al Thuqbah","Al Aqrabiyah","Al Bandariyah","Al Khobar Al Shamaliyah","Al Hamra","Al Olaya","Al Rawdah","Al Muraikabat"] },
      "Al Qatif":     { ar: "القطيف",       districts: ["At Tawbi Dist","Al Qatif Center","Al Buhairah","Saihat","Tarout","Al Awamiyah","Abu Lifa","Al Jesh"] },
      "Tabuk":        { ar: "تبوك",         districts: ["Al Wahah","Al Aziziyah","Al Muruj","Al Rawdah","Al Salam","Al Hamra","King Fahd","Al Riyadh"] },
      "Abha":         { ar: "أبها",         districts: ["Al Mansoorah","Al Aziziyah","Al Wahda","Al Nakheel","Al Rabwa","Al Zahra","Al Maather"] },
      "Taif":         { ar: "الطائف",       districts: ["Al Nuzha","Al Shifa","Al Hawiyah","Al Hada","Al Huwayah","Al Kilo Thamaniya"] },
    }
  },
  "United Arab Emirates": {
    ar: "الإمارات العربية المتحدة",
    cities: {
      "Dubai":        { ar: "دبي",          districts: ["Downtown Dubai","Deira","Bur Dubai","Jumeirah","Al Quoz","Business Bay","DIFC","Al Barsha","Mirdif","Al Karama","Al Nahda","Al Qusais","Al Rashidiya","Silicon Oasis"] },
      "Abu Dhabi":    { ar: "أبوظبي",       districts: ["Al Khalidiyah","Al Mushrif","Al Zaab","Al Muroor","Al Shamkha","Khalifa City","Mohammed Bin Zayed City","Al Reem Island","Al Reef","Al Nahyan"] },
      "Sharjah":      { ar: "الشارقة",      districts: ["Al Nahda","Al Taawun","Al Qasimia","Muwaileh","Al Majaz","Al Khan","Al Mamzar","Halwan"] },
      "Ajman":        { ar: "عجمان",        districts: ["Al Nuaimiyah","Al Rashidiyah","Al Jurf","Al Rumailah","Al Hamidiyah","Al Rawda"] },
      "Ras Al Khaimah": { ar: "رأس الخيمة", districts: ["Al Nakheel","Al Qawasim","Al Uraibi","Al Mamourah","Al Rams"] },
    }
  },
  "Kuwait": {
    ar: "الكويت",
    cities: {
      "Kuwait City":  { ar: "مدينة الكويت", districts: ["Salmiya","Hawalli","Rumaithiya","Mishref","Jabriya","Bayan","Salwa","Fintas","Mangaf","Fahaheel","Ahmadi"] },
      "Al Ahmadi":    { ar: "الأحمدي",      districts: ["Fahaheel","Mangaf","Al Fintas","Al Mahbula","Al Riqqa"] },
    }
  },
  "Bahrain": {
    ar: "البحرين",
    cities: {
      "Manama":       { ar: "المنامة",      districts: ["Adliya","Juffair","Seef","Diplomatic Area","Sanabis","Tubli","Isa Town"] },
      "Muharraq":     { ar: "المحرق",       districts: ["Muharraq Center","Busaiteen","Hidd","Galali"] },
    }
  },
  "Qatar": {
    ar: "قطر",
    cities: {
      "Doha":         { ar: "الدوحة",       districts: ["West Bay","Al Sadd","Al Wakrah","Lusail","Al Waab","Al Khor","Msheireb","The Pearl"] },
      "Al Rayyan":    { ar: "الريان",       districts: ["Al Rayyan Center","Muaither","Al Waab","Gharafa","Al Luqta"] },
    }
  },
  "Oman": {
    ar: "عُمان",
    cities: {
      "Muscat":       { ar: "مسقط",         districts: ["Muttrah","Al Qurum","Ruwi","Al Khuwair","Bausher","Al Maabela","Al Ghubra","Azaiba","Madinat Al Sultan Qaboos"] },
      "Salalah":      { ar: "صلالة",        districts: ["Al Hafah","Salalah Center","Al Nahdah","Itin"] },
    }
  },
  "Jordan": {
    ar: "الأردن",
    cities: {
      "Amman":        { ar: "عمّان",         districts: ["Abdali","Shmeisani","Jabal Al Hussain","Al Wehdat","Sweifieh","Khalda","Al Rabiyeh","Mecca Street","7th Circle","Abdoun"] },
      "Zarqa":        { ar: "الزرقاء",      districts: ["New Zarqa","Old Zarqa","Russeifa"] },
      "Irbid":        { ar: "إربد",         districts: ["Irbid Center","Al Hay Al Janoobi","Al Hay Al Shimali"] },
    }
  },
  "Egypt": {
    ar: "مصر",
    cities: {
      "Cairo":        { ar: "القاهرة",      districts: ["Nasr City","Heliopolis","Maadi","Zamalek","Dokki","Mohandessin","New Cairo","6th of October","Ain Shams","Shubra"] },
      "Alexandria":   { ar: "الإسكندرية",   districts: ["Montaza","Smouha","Gleem","Sidi Gaber","Stanly","Fleming","Raml Station"] },
      "Giza":         { ar: "الجيزة",       districts: ["Haram","Dokki","Agouza","Imbaba","Sheikh Zayed","6th of October"] },
    }
  },
  "Pakistan": {
    ar: "باكستان",
    cities: {
      "Karachi":      { ar: "كراتشي",       districts: ["Clifton","Defence","Gulshan-e-Iqbal","North Nazimabad","Korangi","Malir","Landhi","F.B. Area","Gulberg","S.I.T.E."] },
      "Lahore":       { ar: "لاهور",        districts: ["DHA","Gulberg","Model Town","Johar Town","Bahria Town","Cantt","Iqbal Town","Garden Town"] },
      "Islamabad":    { ar: "إسلام آباد",   districts: ["F-6","F-7","F-8","F-10","G-9","G-10","G-11","I-8","I-9","Blue Area"] },
      "Rawalpindi":   { ar: "راولبندي",     districts: ["Saddar","Chaklala","Bahria Town","DHA","Satellite Town"] },
    }
  },
  "India": {
    ar: "الهند",
    cities: {
      "Mumbai":       { ar: "مومباي",       districts: ["Andheri","Bandra","Colaba","Dadar","Juhu","Kurla","Powai","Thane","Worli","Borivali"] },
      "Delhi":        { ar: "دلهي",         districts: ["Connaught Place","Karol Bagh","Lajpat Nagar","Nehru Place","Rohini","Dwarka","Saket","Vasant Kunj"] },
      "Bangalore":    { ar: "بنغالور",      districts: ["Whitefield","Koramangala","Indiranagar","Jayanagar","Malleswaram","Electronic City"] },
    }
  },
  "USA": {
    ar: "الولايات المتحدة الأمريكية",
    cities: {
      "New York":     { ar: "نيويورك",      districts: ["Manhattan","Brooklyn","Queens","Bronx","Staten Island"] },
      "Los Angeles":  { ar: "لوس أنجلوس",  districts: ["Downtown","Hollywood","Beverly Hills","Santa Monica","Burbank"] },
      "Chicago":      { ar: "شيكاغو",       districts: ["Downtown","Lincoln Park","Hyde Park","Wicker Park","Evanston"] },
    }
  },
  "UK": {
    ar: "المملكة المتحدة",
    cities: {
      "London":       { ar: "لندن",         districts: ["City of London","Westminster","Kensington","Chelsea","Canary Wharf","Shoreditch","Camden","Mayfair"] },
      "Manchester":   { ar: "مانشستر",      districts: ["City Centre","Salford","Trafford","Didsbury","Chorlton"] },
      "Birmingham":   { ar: "برمنغهام",     districts: ["City Centre","Edgbaston","Erdington","Handsworth","Moseley"] },
    }
  },
  "Germany": {
    ar: "ألمانيا",
    cities: {
      "Berlin":       { ar: "برلين",        districts: ["Mitte","Kreuzberg","Prenzlauer Berg","Charlottenburg","Neukölln"] },
      "Munich":       { ar: "ميونخ",        districts: ["Maxvorstadt","Schwabing","Bogenhausen","Pasing","Neuhausen"] },
      "Frankfurt":    { ar: "فرانكفورت",    districts: ["Innenstadt","Sachsenhausen","Bornheim","Gallus","Niederrad"] },
    }
  },
  "China": {
    ar: "الصين",
    cities: {
      "Beijing":      { ar: "بكين",         districts: ["Chaoyang","Dongcheng","Xicheng","Haidian","Fengtai"] },
      "Shanghai":     { ar: "شنغهاي",       districts: ["Pudong","Huangpu","Jing'an","Xuhui","Changning"] },
    }
  },
  "Turkey": {
    ar: "تركيا",
    cities: {
      "Istanbul":     { ar: "إسطنبول",      districts: ["Beyoglu","Kadikoy","Besiktas","Sisli","Fatih","Uskudar","Maltepe"] },
      "Ankara":       { ar: "أنقرة",        districts: ["Cankaya","Kecioren","Yenimahalle","Altindag","Etimesgut"] },
    }
  },
};
