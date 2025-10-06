AI Agent Controller
AI Agent Controller este o aplicație web puternică, construită cu Flask și Socket.IO, care permite unui agent AI (motorizat de modele LLM precum Gemini sau cele rulate local cu Ollama) să interacționeze și să execute comenzi pe un sistem de operare la distanță prin SSH.
Aplicația oferă o interfață completă pentru a defini obiective, a supraveghea execuția în timp real și a interveni atunci când este necesar, transformând sarcinile complexe de administrare de sistem într-un proces automatizat și controlat.
✨ Caracteristici Principale
Compatibilitate Duală LLM: Suport nativ atât pentru Google Gemini (API Cloud), cât și pentru orice model rulat local prin Ollama.
Interfață Web Reactivă: Monitorizează în timp real log-urile agentului și output-ul terminalului de pe sistemul remote.
Moduri de Execuție Flexibile:
Independent Mode: Agentul rulează autonom pentru a-și îndeplini obiectivul, fără intervenție.
Assisted Mode: Fiecare comandă generată de AI este prezentată utilizatorului pentru aprobare, modificare sau respingere, oferind un control total și un plus de siguranță.
Control Dinamic al Sarcinii: Poți pune pe pauză, relua sau opri complet execuția agentului în orice moment. Obiectivul și modul de execuție pot fi modificate în timpul unei pauze.
Management al Conexiunilor și Sesiunilor: Salvează și încarcă configurații de conexiune și sesiuni complete ale agentului (istoric, OS info) pentru a relua lucrul mai târziu.
Securitate Integrată:
Generează automat chei SSH pentru conexiuni securizate.
Include un mecanism de deploy automat al cheii publice pe sistemul remote.
Timeout de siguranță pentru a preveni blocajele în modul asistat.
Containerizare cu Docker: Proiectul este complet containerizat, asigurând o instalare rapidă, consistentă și izolată.
🛠️ Instalare și Rulare (cu Docker)
Cerințe
Un server care rulează o distribuție Linux.
Docker instalat pe server.
Pași
1. Clonează Repository-ul
git clone [https://github.com/your-username/ai-agent-controller.git](https://github.com/your-username/ai-agent-controller.git)
cd ai-agent-controller


2. Configurează Aplicația
Creează fișierul de configurare config.ini pornind de la șablon.
cp config.ini.template config.ini


Acum editează config.ini cu un editor (ex: nano config.ini) și adaugă detaliile tale (cheia API pentru Gemini, URL-ul pentru Ollama etc.).
3. Construiește Imaginea Docker
Această comandă citește dockerfile-ul și creează o imagine locală numită ai-agent-controller.
docker build -t ai-agent-controller .


4. Pornește Containerul
Comanda de mai jos pornește aplicația în background, mapează portul 5001 al serverului la portul 5000 al aplicației și, cel mai important, leagă un director local la directorul /app/keys din container pentru a asigura persistența datelor.
# Creează un director pe gazdă pentru datele persistente (chei, etc.)
mkdir -p ai-agent-data

# Pornește containerul
docker run -d --name ai-agent -p 5001:5000 -v "$(pwd)/ai-agent-data:/app/keys" --restart unless-stopped ai-agent-controller


Notă: -v "$(pwd)/ai-agent-data:/app/keys" este pasul crucial care asigură că cheile SSH, configurațiile de conexiune și sesiunile sunt salvate pe mașina ta gazdă și nu se pierd la repornirea containerului.
🚀 Prima Utilizare
Accesează Interfața: Aplicația este acum accesibilă în browser la adresa http://<IP-ul-serverului>:5001.
Configurează Agentul: În interfață, configurează mai întâi conexiunea la LLM (Ollama sau Gemini).
Configurează Sistemul Remote: Folosește interfața pentru a adăuga IP-ul, utilizatorul și parola sistemului pe care vrei să îl controlezi. Aplicația va folosi parola pentru a instala automat cheia publică SSH pentru autentificare securizată.
⚙️ Comenzi Utile pentru Management Docker
Vezi log-urile în timp real:
docker logs -f ai-agent


Oprește aplicația:
docker stop ai-agent


Pornește aplicația (după ce a fost oprită):
docker start ai-agent


Actualizează aplicația:
Oprește și șterge containerul vechi (docker stop ai-agent, docker rm ai-agent).
Actualizează codul sursă (git pull).
Reconstruiește imaginea (docker build -t ai-agent-controller .).
Pornește din nou containerul cu aceeași comandă docker run. Datele tale vor fi păstrate datorită volumului montat.
