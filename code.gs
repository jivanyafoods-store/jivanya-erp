function doPost(e) {
  try {
    var p = e.parameter || {};
    var ss = SpreadsheetApp.getActiveSpreadsheet();
    
    // 1. User Registration / Profile Update Action
    if (p.action === "register_user") {
      var userSheet = ss.getSheetByName("Users");
      if (!userSheet) {
        userSheet = ss.insertSheet("Users");
        userSheet.appendRow(["Phone", "Name", "Pin", "Address", "City", "Pincode", "CreatedDate"]);
      }
      
      var cleanPhone = String(p.phone || "").replace(/[^0-9]/g, '');
      var data = userSheet.getDataRange().getValues();
      var foundRow = -1;
      
      for (var u = 1; u < data.length; u++) {
        var existingPhone = String(data[u][0]).replace(/[^0-9]/g, '');
        if (existingPhone === cleanPhone) {
          foundRow = u + 1;
          break;
        }
      }
      
      if (foundRow !== -1) {
        userSheet.getRange(foundRow, 2, 1, 5).setValues([[
          p.name || "",
          p.pin || data[foundRow - 1][2],
          p.address || "",
          p.city || "",
          p.pincode || ""
        ]]);
        return ContentService.createTextOutput(JSON.stringify({ status: "UPDATED" })).setMimeType(ContentService.MimeType.JSON);
      } else {
        userSheet.appendRow([
          cleanPhone,
          p.name || "",
          p.pin || "1234",
          p.address || "",
          p.city || "",
          p.pincode || "",
          new Date()
        ]);
        return ContentService.createTextOutput(JSON.stringify({ status: "CREATED" })).setMimeType(ContentService.MimeType.JSON);
      }
    }

    // 2. Customer Review Action
    if (p.action === "review") {
      var revSheet = ss.getSheetByName("Reviews_UGC") || ss.getSheetByName("Reviews");
      if (!revSheet) {
        revSheet = ss.insertSheet("Reviews_UGC");
        revSheet.appendRow(["Date", "CustomerName", "City", "Product", "Rating", "ReviewText", "Approved"]);
      }
      revSheet.appendRow([
        new Date(),
        p.name || "",
        p.city || "",
        p.product || "",
        p.rating || "5",
        p.comment || "",
        "YES"
      ]);
      return ContentService.createTextOutput("REVIEW_SAVED").setMimeType(ContentService.MimeType.TEXT);
    }

    // 3. New Order Logging
    var orderSheet = ss.getSheetByName("Orders") || ss.getSheets()[0];
    var randomId = "ORD-" + Math.floor(1000 + Math.random() * 9000);
    var now = Utilities.formatDate(new Date(), "Asia/Kolkata", "yyyy-MM-dd HH:mm");
    var fullAddress = (p.address || "") + (p.city ? ", " + p.city : "");
    var isGift = (p.notes && p.notes.indexOf("Gift") !== -1) ? "YES" : "NO";

    orderSheet.appendRow([
      randomId,
      now,
      p.name || "",
      p.phone || "",
      fullAddress,
      p.pincode || "",
      p.items || "",
      p.weight || 0,
      p.grandTotal || 0,
      "UPI / Direct",
      "Pending Verification",
      "Processing",
      "India Post",
      "-",
      isGift,
      p.notes || ""
    ]);
    
    return ContentService.createTextOutput("ORDER_SAVED").setMimeType(ContentService.MimeType.TEXT);
  } catch (err) {
    return ContentService.createTextOutput("ERROR: " + err.toString()).setMimeType(ContentService.MimeType.TEXT);
  }
}

function doGet(e) {
  var action = (e && e.parameter && e.parameter.action) || "";
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  
  if (action === "login_user") {
    var checkPhone = String(e.parameter.phone || "").replace(/[^0-9]/g, '');
    var checkPin = String(e.parameter.pin || "").trim();
    var userSheet = ss.getSheetByName("Users");
    
    if (!userSheet) {
      return ContentService.createTextOutput(JSON.stringify({ status: "NO_USERS_TABLE" })).setMimeType(ContentService.MimeType.JSON);
    }
    
    var users = userSheet.getDataRange().getValues();
    for (var i = 1; i < users.length; i++) {
      var uPhone = String(users[i][0]).replace(/[^0-9]/g, '');
      var uPin = String(users[i][2]).trim();
      
      if (uPhone === checkPhone) {
        if (checkPin && uPin !== checkPin) {
          return ContentService.createTextOutput(JSON.stringify({ status: "WRONG_PIN" })).setMimeType(ContentService.MimeType.JSON);
        }
        
        var profile = {
          status: "SUCCESS",
          phone: uPhone,
          name: users[i][1] || "",
          address: users[i][3] || "",
          city: users[i][4] || "",
          pincode: users[i][5] || ""
        };
        return ContentService.createTextOutput(JSON.stringify(profile)).setMimeType(ContentService.MimeType.JSON);
      }
    }
    return ContentService.createTextOutput(JSON.stringify({ status: "NOT_FOUND" })).setMimeType(ContentService.MimeType.JSON);
  }
  
  if (action === "track") {
    var searchPhone = (e.parameter.phone || "").replace(/[^0-9]/g, '');
    var sheet = ss.getSheetByName("Orders") || ss.getSheets()[0];
    var data = sheet.getDataRange().getValues();
    
    if (data.length <= 1) {
      return ContentService.createTextOutput(JSON.stringify({ status: "NOT_FOUND" }))
        .setMimeType(ContentService.MimeType.JSON);
    }
    
    var headers = data[0];
    var phoneIdx = -1, statusIdx = -1, courierIdx = -1, trackIdx = -1;
    
    for (var h = 0; h < headers.length; h++) {
      var head = String(headers[h] || "").trim().toLowerCase();
      if (head.indexOf("phone") !== -1 || head.indexOf("mobile") !== -1) phoneIdx = h;
      if (head.indexOf("status") !== -1) statusIdx = h;
      if (head.indexOf("courier") !== -1 || head.indexOf("partner") !== -1) courierIdx = h;
      if (head.indexOf("track") !== -1 || head.indexOf("docket") !== -1 || head.indexOf("awb") !== -1) trackIdx = h;
    }
    
    if (phoneIdx === -1) phoneIdx = 3;
    if (statusIdx === -1) statusIdx = 11;
    if (courierIdx === -1) courierIdx = 12;
    if (trackIdx === -1) trackIdx = 13;
    
    for (var j = data.length - 1; j >= 1; j--) {
      var rowPhone = String(data[j][phoneIdx] || "").replace(/[^0-9]/g, '');
      if (rowPhone && searchPhone && (rowPhone.indexOf(searchPhone) !== -1 || searchPhone.indexOf(rowPhone) !== -1)) {
        var curStatus = String(data[j][statusIdx] || "Processing").trim();
        var curCourier = String(data[j][courierIdx] || "India Post").trim();
        var curTrack = String(data[j][trackIdx] || "").trim();
        
        if (curTrack === "-") curTrack = "";
        if (!curCourier || curCourier === "-" || curCourier.toLowerCase() === "dispatched") {
          curCourier = "India Post";
        }
        
        var result = {
          status: "FOUND",
          orderStatus: curStatus,
          courier: curCourier,
          trackingNo: curTrack
        };
        return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
      }
    }
    return ContentService.createTextOutput(JSON.stringify({ status: "NOT_FOUND" })).setMimeType(ContentService.MimeType.JSON);
  }
  
  return ContentService.createTextOutput("JIVANYA_API_ACTIVE").setMimeType(ContentService.MimeType.TEXT);
}

// =====================================================================
// AUTOMATIC PRIVATE COSTING & SUPABASE CATALOG SYNC ENGINE
// =====================================================================
const SUPABASE_URL = "https://kslgapyssopepcieujgq.supabase.co";
const SUPABASE_KEY = "sb_publishable_-_Lcmap3PWsl9XPjMq1Otg_XdjrOucW";

function cleanStr(val) {
  return (val || "").toString().toLowerCase().replace(/[^a-z0-9]/g, "");
}

function parseVariantMultiplier(vText, unitType) {
  let v = (vText || "").toString().toLowerCase().trim();
  unitType = (unitType || "kg").toLowerCase().trim();
  
  if (unitType === "no" || unitType === "pack" || unitType === "piece") {
    return { multiplier: 1.0, weightG: 250.0 };
  }
  
  let matchKg = v.match(/([\d\.]+)\s*kg/);
  if (matchKg) {
    let val = parseFloat(matchKg[1]);
    return { multiplier: val, weightG: val * 1000.0 };
  }
  
  let matchL = v.match(/([\d\.]+)\s*(?:litre|liter)/);
  if (matchL) {
    let val = parseFloat(matchL[1]);
    return { multiplier: val, weightG: val * 1000.0 };
  }
  
  let matchGm = v.match(/([\d\.]+)\s*(?:gm|g)\b/);
  if (matchGm) {
    let val = parseFloat(matchGm[1]);
    return { multiplier: val / 1000.0, weightG: val };
  }
  
  let matchMl = v.match(/([\d\.]+)\s*ml\b/);
  if (matchMl) {
    let val = parseFloat(matchMl[1]);
    return { multiplier: val / 1000.0, weightG: val };
  }
  
  let matchNum = v.match(/([\d\.]+)/);
  if (matchNum) {
    let num = parseFloat(matchNum[1]);
    if (num <= 5) return { multiplier: num, weightG: num * 1000.0 };
    return { multiplier: num / 1000.0, weightG: num };
  }
  
  return { multiplier: 0.5, weightG: 500.0 };
}

function syncRowToSupabase(e) {
  const ss = (e && e.source) ? e.source : SpreadsheetApp.getActiveSpreadsheet();
  
  // Public sheet (case insensitive)
  let pubSheet = ss.getSheetByName("Products") || ss.getSheetByName("products") || ss.getSheets()[0];
  let costSheet = ss.getSheetByName("Confidential_Costing");
  
  if (!costSheet) {
    Logger.log("❌ Confidential_Costing sheet nahi mili!");
    return;
  }
  
  // 1. Build Private Costing Map
  let costMap = {};
  const costData = costSheet.getDataRange().getValues();
  const cHeaders = costData[0].map(h => cleanStr(h));
  
  for (let r = 1; r < costData.length; r++) {
    let row = costData[r];
    let obj = {};
    cHeaders.forEach((h, idx) => { obj[h] = row[idx]; });
    
    let pNameKey = cleanStr(obj["productname"]);
    if (pNameKey) {
      costMap[pNameKey] = {
        unitType: (obj["unittype"] || "kg").toString().trim().toLowerCase(),
        purchaseRate: parseFloat(obj["purchaserateperunit"]) || 0.0,
        wastagePct: parseFloat(obj["wastagepct"]) >= 0 ? parseFloat(obj["wastagepct"]) : 3.0,
        packagingCost: parseFloat(obj["packagingcost"]) || 0.0,
        courierFreight: parseFloat(obj["courierfreight"]) || 0.0,
        adSpend: parseFloat(obj["adspend"]) >= 0 ? parseFloat(obj["adspend"]) : 0.0,
        rtoRiskPct: parseFloat(obj["rtoriskpct"]) >= 0 ? parseFloat(obj["rtoriskpct"]) : 4.0,
        gstPct: parseFloat(obj["gstpct"]) >= 0 ? parseFloat(obj["gstpct"]) : 5.0,
        targetMarginPct: parseFloat(obj["targetmarginpct"]) >= 5 ? parseFloat(obj["targetmarginpct"]) : 30.0
      };
    }
  }

  // 2. Read Products
  const pubData = pubSheet.getDataRange().getValues();
  const pubHeaders = pubData[0].map(h => cleanStr(h));
  
  let pIdIdx = pubHeaders.indexOf("productid");
  let pNameIdx = pubHeaders.indexOf("productname");
  let catIdx = pubHeaders.indexOf("subcategory") !== -1 ? pubHeaders.indexOf("subcategory") : pubHeaders.indexOf("category");
  let fssaiIdx = pubHeaders.indexOf("fssailicno");
  let inStockIdx = pubHeaders.indexOf("instock");
  let varIdx = pubHeaders.indexOf("variants");

  let records = [];

  for (let i = 1; i < pubData.length; i++) {
    let row = pubData[i];
    let pId = (row[pIdIdx] || "").toString().trim();
    let pName = (row[pNameIdx] || "").toString().trim();
    let cat = catIdx !== -1 ? (row[catIdx] || "").toString().trim() : "Grocery";
    let fssai = fssaiIdx !== -1 ? (row[fssaiIdx] || "12724999000123").toString().trim() : "12724999000123";
    let inStock = inStockIdx !== -1 ? ((row[inStockIdx] || "").toString().trim().toUpperCase() === "YES") : true;
    let variantsStr = varIdx !== -1 ? (row[varIdx] || "").toString().trim() : "";

    if (!pId || !variantsStr) continue;

    let pKey = cleanStr(pName);
    let costConf = costMap[pKey] || {
      unitType: "kg",
      purchaseRate: 0.0,
      wastagePct: 3.0,
      packagingCost: 0.0,
      courierFreight: 0.0,
      adSpend: 0.0,
      rtoRiskPct: 4.0,
      gstPct: 5.0,
      targetMarginPct: 30.0
    };

    let variantList = variantsStr.split(",");
    variantList.forEach(item => {
      item = item.trim();
      if (!item) return;

      let parts = item.split(":");
      let vName = parts[0].trim();
      let listedPrice = parts.length >= 2 ? parseFloat(parts[1].toString().replace(/[^\d\.]/g, "")) || 100.0 : 100.0;

      let parsed = parseVariantMultiplier(vName, costConf.unitType);
      let weightG = parsed.weightG;
      let multiplier = parsed.multiplier;

      let cleanVariant = vName.replace(/[^A-Za-z0-9]/g, "").toUpperCase();
      let sku = `${pId}-${cleanVariant}`;
      let fullTitle = `${pName} (${vName})`;

      let l = 15.0, w = 10.0, h = 3.0, autoPack = 8.0, autoCourier = 45.0;
      if (weightG > 1000) { l = 30.0; w = 20.0; h = 10.0; autoPack = 22.0; autoCourier = 130.0; }
      else if (weightG > 500) { l = 24.0; w = 16.0; h = 6.0; autoPack = 12.0; autoCourier = 75.0; }
      else if (weightG > 250) { l = 18.0; w = 12.0; h = 4.0; autoPack = 9.0; autoCourier = 50.0; }

      let finalPackCost = costConf.packagingCost > 0 ? costConf.packagingCost : autoPack;
      let finalCourierCost = costConf.courierFreight > 0 ? costConf.courierFreight : autoCourier;

      // Base Raw Calculation
      let baseRaw = costConf.purchaseRate > 0 ? (costConf.purchaseRate * multiplier) : (listedPrice * 0.60);
      let effectiveRaw = baseRaw + (baseRaw * (costConf.wastagePct / 100.0));
      let baseOperational = effectiveRaw + finalPackCost + finalCourierCost + costConf.adSpend + 5.0;
      let totalLandedCost = baseOperational + (baseOperational * (costConf.rtoRiskPct / 100.0));

      let preTaxPrice = totalLandedCost / (1.0 - (costConf.targetMarginPct / 100.0));
      let finalFloorPrice = Math.round((preTaxPrice + (preTaxPrice * (costConf.gstPct / 100.0))) * 100) / 100;

      records.push({
        sku: sku,
        title: fullTitle.substring(0, 250),
        category: cat.substring(0, 90),
        dead_weight_grams: weightG,
        length_cm: l,
        width_cm: w,
        height_cm: h,
        cogs_price: Math.round(baseRaw * 100) / 100,
        packaging_cost: finalPackCost,
        min_floor_price: finalFloorPrice,
        fssai_license: fssai,
        stock_hathras: inStock ? 50 : 0,
        stock_agra: inStock ? 10 : 0,
        stock_delhi: inStock ? 10 : 0,
        stock_mumbai: inStock ? 5 : 0
      });
    });
  }

  // Batch Push to Supabase
  for (let c = 0; c < records.length; c += 40) {
    let chunk = records.slice(c, c + 40);
    let options = {
      method: "post",
      headers: {
        "apikey": SUPABASE_KEY,
        "Authorization": "Bearer " + SUPABASE_KEY,
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates"
      },
      payload: JSON.stringify(chunk),
      muteHttpExceptions: true
    };
    let response = UrlFetchApp.fetch(`${SUPABASE_URL}/rest/v1/master_catalog?on_conflict=sku`, options);
    Logger.log("Batch " + c + " Status: " + response.getResponseCode());
  }
  Logger.log("✅ Finished! Total " + records.length + " SKUs updated in Supabase.");
}

function forceSyncCosting() {
  syncRowToSupabase(null);
  Logger.log("🚀 Sync Completed Successfully!");
}