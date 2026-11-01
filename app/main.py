from __future__ import annotations
import os, re, json, ipaddress, socket, logging
from typing import Any
from urllib.parse import urlparse
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log=logging.getLogger("digital_dark_spot")
app=FastAPI(title="Digital Dark Spot API", version="2.0.0", description="Evidence-oriented research analysis API")
app.mount("/assets", StaticFiles(directory="assets"), name="assets")
app.add_middleware(CORSMiddleware, allow_origins=os.getenv("CORS_ORIGINS","*").split(","), allow_credentials=False, allow_methods=["GET","POST"], allow_headers=["*"])
MAX_TEXT=18000; MAX_URLS=8; MAX_FILE=10*1024*1024

class ChatTurn(BaseModel):
    role: str
    text: str = Field(max_length=12000)
class ChatRequest(BaseModel):
    message: str = Field(min_length=1,max_length=12000)
    history: list[ChatTurn] = Field(default_factory=list,max_length=24)

@app.get("/", include_in_schema=False)
def home(): return FileResponse("index.html")
@app.get("/api")
def root(): return {"name":"Digital Dark Spot API","version":"2.0.0","docs":"/docs","health":"/health"}
@app.get("/health")
def health(): return {"ok":True,"service":"digital-dark-spot","version":"2.0.0"}

def safe_url(url:str)->str:
    try:
        p=urlparse(url.strip())
        if p.scheme not in ("http","https") or not p.hostname or p.username or p.password: raise ValueError
        host=p.hostname.lower()
        if host in ("localhost","localhost.localdomain") or host.endswith(".local"): raise ValueError
        try: ip=ipaddress.ip_address(host)
        except ValueError:
            for info in socket.getaddrinfo(host,None):
                ip=ipaddress.ip_address(info[4][0])
                if not ip.is_global: raise ValueError
        else:
            if not ip.is_global: raise ValueError
        return url.strip()
    except Exception: raise HTTPException(400, f"Unsafe or invalid source URL: {url}")

async def fetch_source(url:str)->dict[str,Any]:
    url=safe_url(url)
    headers={"User-Agent":"DigitalDarkSpotResearch/2.0"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15,connect=7),follow_redirects=True,max_redirects=4,headers=headers) as client:
            r=await client.get(url);r.raise_for_status()
            # Validate the final redirect target too, to reduce SSRF risk.
            safe_url(str(r.url))
            ctype=r.headers.get("content-type","").lower()
            if "text/html" in ctype or "text/plain" in ctype:
                soup=BeautifulSoup(r.text,"html.parser")
                for tag in soup(["script","style","nav","footer","header","noscript","svg"]):tag.decompose()
                title=soup.title.get_text(" ",strip=True) if soup.title else str(r.url)
                main=soup.find("main") or soup.find("article") or soup.body or soup
                text=re.sub(r"\s+"," ",main.get_text(" ",strip=True))[:MAX_TEXT]
            elif "application/pdf" in ctype or str(r.url).lower().endswith(".pdf"):
                import fitz
                doc=fitz.open(stream=r.content,filetype="pdf");title=str(r.url);text=" ".join(page.get_text() for page in doc)[:MAX_TEXT]
            else: raise ValueError(f"Unsupported content type: {ctype or 'unknown'}")
            if len(text.strip())<80:raise ValueError("Not enough readable text was found.")
            return {"url":str(r.url),"title":title[:300],"text":text,"status":"read"}
    except HTTPException:raise
    except Exception as e:
        log.warning("Source retrieval failed (%s): %s",url,e)
        return {"url":url,"title":url,"text":"","status":"unavailable","error":str(e)[:300]}

async def parse_upload(file:UploadFile)->dict:
    name=(file.filename or "uploaded-file").strip();ext=os.path.splitext(name)[1].lower()
    if ext not in {".txt",".pdf",".docx"}:raise HTTPException(400,f"Unsupported file type: {name}")
    raw=await file.read(MAX_FILE+1)
    if len(raw)>MAX_FILE:raise HTTPException(413,f"{name} exceeds 10 MB.")
    try:
        if ext==".txt":text=raw.decode("utf-8-sig",errors="replace")
        elif ext==".pdf":
            import fitz
            doc=fitz.open(stream=raw,filetype="pdf");text=" ".join(p.get_text() for p in doc)
        else:
            from docx import Document
            import io
            doc=Document(io.BytesIO(raw));text=" ".join(p.text for p in doc.paragraphs)
    except Exception as e:raise HTTPException(400,f"Could not read {name}: {e}")
    return {"url":f"upload://{name}","title":name,"text":text[:MAX_TEXT],"status":"read"}

async def gemini_json(prompt:str)->dict|None:
    key=os.getenv("GEMINI_API_KEY")
    if not key:return None
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash")
    url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload={"contents":[{"role":"user","parts":[{"text":prompt}]}],"generationConfig":{"temperature":0.2,"responseMimeType":"application/json","maxOutputTokens":5000}}
    try:
        async with httpx.AsyncClient(timeout=50) as client:
            r=await client.post(url,headers={"x-goog-api-key":key},json=payload);r.raise_for_status();data=r.json()
        txt="".join(p.get("text","") for p in data["candidates"][0]["content"]["parts"])
        txt=re.sub(r"^\`\`\`(?:json)?|\`\`\`$","",txt.strip(),flags=re.M).strip()
        return json.loads(txt)
    except Exception as e:log.exception("Gemini analysis error: %s",e);return None

def fallback_analysis(question:str,sources:list[dict])->dict:
    claims=[]
    for i,s in enumerate(sources):
        for sent in re.split(r"(?<=[.!?])\s+",s["text"]):
            sent=sent.strip()
            if 45<=len(sent)<=420 and re.search(r"\b(is|are|was|were|will|must|can|should|may|has|have|reported|according|percent|%|\d)\b",sent,re.I):
                claims.append({"claim":sent,"source_url":s["url"],"source_title":s["title"],"source_index":i})
            if len(claims)>=25:break
    gaps=[{"question":f"What recent or authoritative evidence answers: {question}?","reason":"The supplied material may not cover all aspects of the research question.","priority":"high"},{"question":"Are publication dates, methodology, sample sizes, or limitations missing?","reason":"Context is needed to assess reliability.","priority":"medium"}]
    checklist=["Confirm publication date and whether the source is current.","Verify important claims against an independent authoritative source.","Check methodology, definitions, and stated limitations.","Look for perspectives or evidence missing from the supplied sources."]
    return {"summary":"Sources were retrieved, but AI semantic comparison is unavailable. Findings are preliminary and require human verification.","claims":claims[:20],"gaps":gaps,"conflicts":[],"verification_checklist":checklist,"confidence":"low","analysis_mode":"heuristic_fallback"}

@app.post("/analyze")
async def analyze(question:str=Form(...),sources:str=Form("[]"),documents:list[UploadFile]=File(default=[])):
    question=question.strip()
    if len(question)<8:raise HTTPException(400,"Please enter a research question with at least 8 characters.")
    try:urls=json.loads(sources)
    except Exception:raise HTTPException(400,"Sources must be a JSON array of URLs.")
    if not isinstance(urls,list) or len(urls)>8:raise HTTPException(400,"Add no more than 8 URLs.")
    if not urls and not documents:raise HTTPException(400,"Add at least one URL or upload a document.")
    collected=[]
    for u in urls:
        if not isinstance(u,str):continue
        collected.append(await fetch_source(u))
    for f in documents:
        if f.filename:collected.append(await parse_upload(f))
    usable=[s for s in collected if s["text"]]
    if not usable:raise HTTPException(422,"No readable sources could be extracted. Check URLs or file formats and try again.")
    excerpts=[{"source_index":i,"url":s["url"],"title":s["title"],"text":s["text"][:9000]} for i,s in enumerate(usable)]
    prompt=("You are an evidence-oriented research analyst. Analyze only the supplied excerpts. Do not invent sources or claim unsupported facts. Return strict JSON with keys summary, claims (objects claim, source_index, source_url, source_title), gaps (objects question, reason, priority), conflicts (objects claim_a, claim_b, explanation, source_indices, severity), verification_checklist (strings), confidence. Claims must be supported. A conflict means statements appear incompatible, not merely different. Note uncertainty and outdatedness.\nRESEARCH QUESTION: "+question+"\nSOURCES:\n"+json.dumps(excerpts,ensure_ascii=False))
    ai=await gemini_json(prompt)
    if ai and isinstance(ai,dict):
        valid_urls={s["url"] for s in usable};claims=[]
        for c in ai.get("claims",[])[:30]:
            if not isinstance(c,dict):continue
            u=c.get("source_url");idx=c.get("source_index")
            if isinstance(idx,int) and 0<=idx<len(usable):u=usable[idx]["url"]
            if u not in valid_urls:continue
            claims.append({"claim":str(c.get("claim",""))[:1000],"source_url":u,"source_title":str(c.get("source_title",next((s["title"] for s in usable if s["url"]==u),u)))[:300]})
        result={"summary":str(ai.get("summary","Analysis completed."))[:3000],"claims":claims,"gaps":[g for g in ai.get("gaps",[])[:20] if isinstance(g,dict)],"conflicts":[c for c in ai.get("conflicts",[])[:20] if isinstance(c,dict)],"verification_checklist":[str(x)[:500] for x in ai.get("verification_checklist",[])[:20]],"confidence":ai.get("confidence","medium"),"analysis_mode":"gemini"}
    else:result=fallback_analysis(question,usable)
    return {"ok":True,"question":question,"sources":[{"url":s["url"],"title":s["title"],"status":s["status"],"error":s.get("error")} for s in collected],"source_count":len(usable),"unavailable_count":len(collected)-len(usable),"analysis":result,"message":result["summary"]}

@app.post("/chat")
async def chat(req:ChatRequest):
    key=os.getenv("GEMINI_API_KEY")
    if not key:raise HTTPException(503,"AI chat is not configured. Set GEMINI_API_KEY in your environment.")
    contents=[]
    for turn in req.history[-12:]:
        role="model" if turn.role.lower() in ("model","assistant") else "user"
        if turn.text.strip():contents.append({"role":role,"parts":[{"text":turn.text.strip()}]})
    contents.append({"role":"user","parts":[{"text":req.message.strip()}]})
    payload={"systemInstruction":{"parts":[{"text":"You are Digital Dark Spot, a research and learning assistant. Be concise, structured and honest about uncertainty. Never invent citations or claim to have read sources that were not provided. Encourage verification of high-stakes/current facts."}]},"contents":contents,"generationConfig":{"temperature":0.4,"maxOutputTokens":1800}}
    model=os.getenv("GEMINI_MODEL","gemini-2.5-flash");url=f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r=await client.post(url,headers={"x-goog-api-key":key},json=payload)
            if r.status_code==429:raise HTTPException(429,"Gemini rate limit reached. Please wait and retry.")
            r.raise_for_status();data=r.json();answer="".join(p.get("text","") for p in data["candidates"][0]["content"]["parts"])
            if not answer:raise ValueError("Empty response")
            return {"ok":True,"answer":answer,"model":model}
    except HTTPException:raise
    except Exception:log.exception("Chat request failed");raise HTTPException(502,"Gemini request failed. Check API key, quota, and server logs.")
