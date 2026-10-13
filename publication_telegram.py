import asyncio
import base64
import io
import json
import os
import random
import requests
from datetime import datetime, timezone
from PIL import Image
from supabase import Client, create_client
from telegram import Bot
from telegram.request import HTTPXRequest

# ==========================================
# 0. CHARGEMENT INITIAL DES CONFIGS DEPUIS SUPABASE
# ==========================================
print("Connexion à Supabase pour récupérer la configuration depuis les tables dédiées...")

SUPABASE_URL_DEFAUT = "https://acdqyhafpzrlrsheuxgq.supabase.co"
SUPABASE_KEY_DEFAUT = "sb_publishable_ZbIRDJWZ1uGxV4ZsvsFxKg_0SoG4TOU"

supabase_init: Client = create_client(SUPABASE_URL_DEFAUT, SUPABASE_KEY_DEFAUT)

def lire_table_supabase(nom_table, colonne):
    """Fonction utilitaire pour lire une valeur unique dans une table dédiée."""
    try:
        res = supabase_init.table(nom_table).select(colonne).limit(1).execute()
        if res.data and res.data[0].get(colonne):
            val = str(res.data[0][colonne]).strip()
            val = val.strip('"').strip("'").strip()
            return val
    except Exception as e:
        print(f"⚠️ Erreur lors de la lecture de {nom_table} : {e}")
    return None

url_lue = lire_table_supabase("table_url", "valeur")
SUPABASE_URL = url_lue if (url_lue and url_lue.startswith("http")) else SUPABASE_URL_DEFAUT

key_lue = lire_table_supabase("table_key", "valeur")
SUPABASE_KEY = key_lue if key_lue else SUPABASE_KEY_DEFAUT

if SUPABASE_URL != SUPABASE_URL_DEFAUT or SUPABASE_KEY != SUPABASE_KEY_DEFAUT:
    supabase_init = create_client(SUPABASE_URL, SUPABASE_KEY)

PROMPTIX_KEY = lire_table_supabase("table_cle", "valeur")
PAGE_ACCESS_TOKEN = lire_table_supabase("table_token_facebook", "valeur")
TELEGRAM_BOT_TOKEN = lire_table_supabase("table_telegram_token", "valeur")
TELEGRAM_CHAT_ID = lire_table_supabase("table_telegram_chat_id", "valeur")

# Récupération dynamique de l'ID de la page Facebook et de l'ID Instagram Business
PAGE_ID = lire_table_supabase("table_page_id", "valeur") or lire_table_supabase("table_page_id", "page_id") or "111157353629841"
INSTAGRAM_BUSINESS_ID = lire_table_supabase("instagram_config", "valeur") or lire_table_supabase("instagram_config", "instagram_business_id") or "17841451463230647"

PROMPTIX_URL = "https://promptix.tn/api/v1/chat/completions"

MODELE_TEXTE = "google/gemini-3.8-flash"
MODELE_IMAGE = "google/gemini-3.1-flash-image-preview"

GRAPH_API_VERSION = "v26.0"
BASE_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
GRAPH_URL_FB = f"{BASE_GRAPH_URL}/{PAGE_ID}/photos"

BUCKET_NAME = "patisseries"
FICHIER_RESULTAT = "resultat.png"

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def recuperer_restriction_supabase(nom_table):
    """Récupère le contenu textuel d'une consigne depuis sa table dédiée."""
    try:
        res = supabase.table(nom_table).select("contenu").limit(1).execute()
        if res.data:
            return res.data[0]["contenu"]
    except Exception as e:
        print(f"⚠️ Erreur récupération restriction de {nom_table} : {e}")
    return ""


def verifier_plage_horaire():
    """Vérifie si l'heure actuelle du serveur est dans la plage horaire définie dans Supabase."""
    try:
        res = supabase.table("table_horaires_publication").select("*").limit(1).execute()
        if res.data:
            h_debut_str = res.data[0]["heure_debut"]
            h_fin_str = res.data[0]["heure_fin"]

            h_debut = datetime.strptime(h_debut_str, "%H:%M:%S").time()
            h_fin = datetime.strptime(h_fin_str, "%H:%M:%S").time()

            maintenant = datetime.now().time()

            est_dans_plage = h_debut <= maintenant <= h_fin
            print(f"⏰ Plage horaire autorisée : {h_debut} à {h_fin} | Heure actuelle : {maintenant.strftime('%H:%M:%S')}")
            print(f"📌 Dans l'intervalle ? {'OUI' if est_dans_plage else 'NON'}")
            return est_dans_plage
    except Exception as e:
        print(f"⚠️ Erreur vérification plage horaire : {e}")
    return True


def verifier_reponse_telegram_securisee(date_creation_post_utc):
    """
    Interroge Telegram et vérifie qu'une réponse 'oui' ou 'non' a été envoyée
    STRICTEMENT APRÈS la date de création de la proposition dans Supabase.
    """
    url_updates = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        rep_tg = requests.get(url_updates, timeout=30).json()
        if rep_tg.get("ok"):
            resultats = rep_tg.get("result", [])
            for res in reversed(resultats):
                message = res.get("message", {})
                if str(message.get("chat", {}).get("id")) == str(TELEGRAM_CHAT_ID):
                    msg_timestamp = message.get("date")  # Timestamp UNIX en UTC
                    texte_msg = message.get("text", "").strip().lower()

                    if msg_timestamp and date_creation_post_utc:
                        # Vérification que le message est plus récent que la création du post
                        if msg_timestamp > date_creation_post_utc:
                            if texte_msg in ["oui", "valider"]:
                                print(f"🟢 'oui' VALIDE reçu sur Telegram (Envoyé à {msg_timestamp} > Créé à {date_creation_post_utc})")
                                return "oui"
                            elif texte_msg in ["non", "refuser"]:
                                print(f"🔴 'non' VALIDE reçu sur Telegram (Envoyé à {msg_timestamp} > Créé à {date_creation_post_utc})")
                                return "non"
                        else:
                            print(f"⚠️ Ancien message Telegram ignoré (Envoyé à {msg_timestamp} <= Créé à {date_creation_post_utc})")
                            break
    except Exception as e:
        print(f"⚠️ Erreur lors de la lecture sécurisée de Telegram : {e}")
    return ""


def photo_en_base64(img, taille_max):
    copie = img.copy()
    copie.thumbnail((taille_max, taille_max))
    tampon = io.BytesIO()
    copie.save(tampon, format="JPEG", quality=90)
    octets = tampon.getvalue()
    return base64.b64encode(octets).decode("ascii")


def telecharger_image_supabase(supabase_client, bucket, nom_fichier):
    try:
        url = f"{SUPABASE_URL}/storage/v1/object/public/{bucket}/{nom_fichier}"
        rep = requests.get(url, timeout=60)
        if rep.status_code == 200:
            return Image.open(io.BytesIO(rep.content)).convert("RGB")
    except Exception as e:
        print(f"⚠️ Impossible de charger {nom_fichier} : {e}")
    return None


def appeler_promptix(payload):
    return requests.post(
        PROMPTIX_URL,
        headers={"Authorization": "Bearer " + PROMPTIX_KEY},
        json=payload,
        timeout=300,
    )


def extraire_image(message):
    for image in message.get("images") or []:
        url = image.get("image_url", {}).get("url", "")
        if url.startswith("data:image/"):
            return base64.b64decode(url.split(",", 1)[1])
        if url.startswith("http"):
            rep = requests.get(url, timeout=120)
            rep.raise_for_status()
            return rep.content
    return None


def nettoyer_image_storage(image_path):
    """Supprime proprement une image du Storage Supabase pour économiser l'espace."""
    if image_path:
        try:
            supabase.storage.from_(BUCKET_NAME).remove([image_path])
            print(f"🧹 Image '{image_path}' supprimée du Storage Supabase avec succès.")
        except Exception as e:
            print(f"⚠️ Erreur lors du nettoyage de l'image du Storage : {e}")


def generer_nouveau_contenu():
    print("\n-----------------------------------------")
    print(f"🔄 Lancement du cycle créatif piloté par le Directeur Marketing ({MODELE_TEXTE})...")

    articles = supabase.table("articles").select("*").execute().data
    if not articles:
        raise SystemExit("Aucune donnée dans la table 'articles'.")

    sac_de_tirage = []
    for art in articles:
        importance = int(art.get("importance") or 3)
        sac_de_tirage.extend([art] * importance)

    article = random.choice(sac_de_tirage)
    print(f"📌 Article tiré : {article['article']} ({article['categorie']})")

    ref_photo_brut = str(article["ref_photo"]).strip()
    
    extensions_a_tester = []
    if ref_photo_brut.lower().endswith((".jpg", ".jpeg", ".png")):
        extensions_a_tester = [ref_photo_brut]
    else:
        extensions_a_tester = [
            f"{ref_photo_brut}.jpeg",
            f"{ref_photo_brut}.jpg",
            f"{ref_photo_brut}.png"
        ]

    photo = None
    for ref_photo_path in extensions_a_tester:
        photo_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{ref_photo_path}"
        print(f"📥 Tentative de téléchargement : {ref_photo_path}...")
        rep = requests.get(photo_url, timeout=60)
        if rep.status_code == 200:
            try:
                photo = Image.open(io.BytesIO(rep.content)).convert("RGB")
                print(f"✅ Photo chargée avec succès depuis {ref_photo_path}")
                break
            except Exception:
                pass

    if photo is None:
        print(f"⚠️ Impossible de télécharger l'image pour l'article {article['article']}. On retente...")
        return generer_nouveau_contenu()

    print("📥 Téléchargement des logos et récupération des restrictions depuis les tables Supabase...")
    img_logo = telecharger_image_supabase(supabase, BUCKET_NAME, "logo.jpg")
    img_glovo = telecharger_image_supabase(supabase, BUCKET_NAME, "glovo.jpg")
    img_zigzag = telecharger_image_supabase(supabase, BUCKET_NAME, "zigzag.jpg")

    restrictions_image_texte = recuperer_restriction_supabase("table_restriction_image")
    restrictions_texte_publication = recuperer_restriction_supabase("table_restriction_texte")

    print("🧠 Le Directeur Marketing IA analyse le produit et conçoit la stratégie globale...")
    prompt_directeur = f"""
    Agis en tant que Directeur Marketing et Directeur Artistique senior (20 ans d'expérience dans la haute pâtisserie internationale et tunisienne).
    
    Produit :
    - Nom : {article['article']}
    - Catégorie : {article['categorie']}

    {restrictions_texte_publication}

    Mission :
    Crée un plan créatif complet structuré STRICTEMENT avec ces deux balises :

    [DIRECTION_ARTISTIQUE_IMAGE]
    (Décris ici la mise en scène visuelle ultra-détaillée pour la photo : décors, fonds nobles, éclairage studio, ambiance, textures, sans fond blanc artificiel)

    [TEXTE_FACEBOOK]
    (Rédige uniquement ici la publication Facebook/Instagram finale en respectant scrupuleusement toutes les consignes ci-dessus)
    """

    rep_directeur = appeler_promptix(
        {
            "model": MODELE_TEXTE,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_directeur},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/jpeg;base64," + photo_en_base64(photo, 1024)
                            },
                        },
                    ],
                }
            ],
        }
    )

    if rep_directeur.ok:
        contenu_ia = rep_directeur.json()["choices"][0]["message"]["content"].strip()

        if "[TEXTE_FACEBOOK]" in contenu_ia:
            parties = contenu_ia.split("[TEXTE_FACEBOOK]")
            da_partie = parties[0].replace("[DIRECTION_ARTISTIQUE_IMAGE]", "").strip()
            texte_facebook = parties[1].strip()
        else:
            da_partie = contenu_ia
            texte_facebook = contenu_ia

        texte_telegram = (
            f"<b>🎨 Direction Artistique :</b>\n<i>{da_partie}</i>\n\n"
            f"<b>📝 Texte Publication :</b>\n{texte_facebook}"
        )
        texte_final_facebook = texte_facebook
    else:
        print("⚠️ Erreur génération Directeur Marketing, secours...")
        da_partie = "Mets en valeur les textures et l'éclat de cette création dans un décor de haute pâtisserie."
        texte_final_facebook = (
            f"Découvrez notre {article['article']} chez Saveurs Dammak.\n\n"
            f"📍 Route de Saltnia – Km 3,5 · 📞 99 460 036\n#SaveursDammak #Sfax"
        )
        texte_telegram = texte_final_facebook

    print("🎨 Exécution de la retouche visuelle et incrustation du branding...")
    
    contenu_message_image = [
        {"type": "text", "text": f"{restrictions_image_texte}\n\nDirection Artistique globale : {da_partie}"},
        {"type": "text", "text": "IMAGE PRINCIPALE : Creation de patisserie a sublimer."},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(photo, 1024)},
        },
    ]

    if img_logo:
        contenu_message_image.append({"type": "text", "text": "IMAGE LOGO OFFICIEL : C'est le logo 'Saveurs Dammak'. ATTENTION : Supprime complètement son fond carré d'origine. Incruste UNIQUEMENT le texte et le monogramme SD de manière élégante, transparente et fluide en haut à droite (sans aucun cadre rectangulaire ou carré autour)."})
        contenu_message_image.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(img_logo, 512)},
        })

    if img_glovo:
        contenu_message_image.append({"type": "text", "text": "IMAGE PARTENAIRE 1 : Logo Glovo à placer en bas à gauche, taille égale à Zigzag, sans fond carré disgracieux."})
        contenu_message_image.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(img_glovo, 512)},
        })

    if img_zigzag:
        contenu_message_image.append({"type": "text", "text": "IMAGE PARTENAIRE 2 : Logo Zigzag à placer en bas à gauche juste à côté de Glovo, même taille et même hauteur."})
        contenu_message_image.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(img_zigzag, 512)},
        })

    reponse_img = appeler_promptix(
        {
            "model": MODELE_IMAGE,
            "modalities": ["image", "text"],
            "messages": [
                {
                    "role": "user",
                    "content": contenu_message_image,
                }
            ],
        }
    )

    if not reponse_img or not reponse_img.ok:
        print(f"⚠️ Erreur retouche d'image, on retente...")
        return generer_nouveau_contenu()

    message = reponse_img.json().get("choices", [{}])[0].get("message", {})
    image_bytes = extraire_image(message)
    if not image_bytes:
        print("⚠️ Aucune image retournée, on retente...")
        return generer_nouveau_contenu()

    with Image.open(io.BytesIO(image_bytes)) as im:
        im.convert("RGB").save(FICHIER_RESULTAT, format="PNG")

    nom_fichier_storage = f"temp_{random.randint(10000, 99999)}.png"
    print("☁️ Upload temporaire sur Supabase Storage...")
    with open(FICHIER_RESULTAT, "rb") as f:
        supabase.storage.from_(BUCKET_NAME).upload(nom_fichier_storage, f)

    print("💾 Enregistrement dans Supabase...")
    data_insertion = {
        "article_nom": article["article"],
        "categorie": article["categorie"],
        "scenario_titre": "Directeur Marketing & Branding (Tables Supabase Dédiées)",
        "texte_genere": texte_final_facebook,
        "image_path": nom_fichier_storage,
        "statut": "en_attente",
    }
    supabase.table("historique_publications").insert(data_insertion).execute()

    print("✨ Cycle créatif terminé avec succès !")
    return texte_telegram, FICHIER_RESULTAT, nom_fichier_storage


async def envoyer_vers_telegram(texte, image_path):
    request = HTTPXRequest(read_timeout=60, write_timeout=60, connect_timeout=60)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
    
    header = (
        "<b>🍰 Proposition Saveurs Dammak (Tables Dédiées Cloud) :</b>\n\n"
        "<i>💬 Répondez simplement par <b>'oui'</b> pour valider et publier, "
        "ou <b>'non'</b> pour refuser et générer un autre visuel.</i>\n\n"
    )
    message_complet = header + texte

    with Image.open(image_path) as img:
        img.thumbnail((1200, 1200))
        img.save(image_path, "JPEG", quality=85)

    with open(image_path, "rb") as photo_file:
        if len(message_complet) <= 1024:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo_file,
                caption=message_complet,
                parse_mode="HTML",
            )
        else:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo_file,
                caption="<b>🍰 Visuel retouché (voir texte ci-dessous) :</b>",
                parse_mode="HTML",
            )
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message_complet,
                parse_mode="HTML",
            )
    print("📱 Notification envoyée sur Telegram avec succès !")


def publier_sur_facebook(texte, image_path):
    print("\n🚀 Publication sur Facebook en cours...")
    with open(image_path, "rb") as fichier:
        rep_fb = requests.post(
            GRAPH_URL_FB,
            data={
                "access_token": PAGE_ACCESS_TOKEN,
                "message": texte,
                "published": "true",
            },
            files={"source": ("resultat.png", fichier, "image/png")},
            timeout=180,
        )
    return rep_fb.json()


def publier_sur_instagram(texte, image_url):
    print("\n📸 Publication sur Instagram en cours...")
    
    container_endpoint = f"{BASE_GRAPH_URL}/{INSTAGRAM_BUSINESS_ID}/media"
    container_payload = {
        "image_url": image_url,
        "caption": texte,
        "access_token": PAGE_ACCESS_TOKEN
    }
    
    res_container = requests.post(container_endpoint, data=container_payload)
    res_container_json = res_container.json()

    if "id" not in res_container_json:
        print(f"❌ Erreur création conteneur Instagram: {res_container_json}")
        return {"error": res_container_json}

    creation_id = res_container_json["id"]
    print(f"🔹 Conteneur Instagram créé (ID: {creation_id})")

    publish_endpoint = f"{BASE_GRAPH_URL}/{INSTAGRAM_BUSINESS_ID}/media_publish"
    publish_payload = {
        "creation_id": creation_id,
        "access_token": PAGE_ACCESS_TOKEN
    }

    res_publish = requests.post(publish_endpoint, data=publish_payload)
    return res_publish.json()


def executer_publication_facebook_et_instagram(derniere_action):
    """Effectue la publication simultanée sur Facebook et Instagram, puis nettoie le Storage."""
    if not os.path.exists(FICHIER_RESULTAT):
        img_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{derniere_action['image_path']}"
        r = requests.get(img_url)
        with open(FICHIER_RESULTAT, "wb") as f:
            f.write(r.content)

    image_publique_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{derniere_action['image_path']}"
    texte_post = derniere_action["texte_genere"]

    # 1. Publication Facebook
    resultat_fb = publier_sur_facebook(texte_post, FICHIER_RESULTAT)
    succes_fb = "error" not in resultat_fb
    fb_post_id = resultat_fb.get("post_id") or resultat_fb.get("id") if succes_fb else None

    if succes_fb:
        print(f"🎉 Publication Facebook réussie ! ID : {fb_post_id}")
    else:
        print("❌ Échec Facebook :", resultat_fb)

    # 2. Publication Instagram
    resultat_ig = publier_sur_instagram(texte_post, image_publique_url)
    succes_ig = "error" not in resultat_ig
    ig_post_id = resultat_ig.get("id") if succes_ig else None

    if succes_ig:
        print(f"🎉 Publication Instagram réussie ! ID : {ig_post_id}")
    else:
        print("❌ Échec Instagram :", resultat_ig)

    if succes_fb or succes_ig:
        supabase.table("historique_publications").update({"statut": "termine"}).eq("id", derniere_action["id"]).execute()
        nettoyer_image_storage(derniere_action.get("image_path"))

        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        msg_recap = f"🎉 Publication réussie !\n📘 Facebook: {'✅' if succes_fb else '❌'}\n📸 Instagram: {'✅' if succes_ig else '❌'}"
        asyncio.run(
            bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg_recap,
            )
        )
        return True
    else:
        print("❌ Échec global des publications Facebook et Instagram.")
        return False


def main():
    print("🔍 Récupération de la dernière publication dans Supabase...")

    historique = (
        supabase.table("historique_publications")
        .select("*")
        .order("id", desc=True)
        .limit(1)
        .execute()
        .data
    )

    if not historique:
        print("ℹ️ Aucune publication trouvée dans la table. Lancement du premier cycle créatif...")
        texte_nouveau, image_nouveau, _ = generer_nouveau_contenu()
        asyncio.run(envoyer_vers_telegram(texte_nouveau, image_nouveau))
        return

    derniere_action = historique[0]
    statut_actuel = derniere_action.get("statut", "termine")

    # Extraire et convertir le timestamp de création de la publication
    created_at_str = derniere_action.get("created_at")
    date_creation_utc = None
    if created_at_str:
        try:
            # Conversion ISO 8601 (ex: "2026-10-13T16:10:00+00:00") en timestamp UNIX
            dt = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            date_creation_utc = int(dt.timestamp())
        except Exception as e:
            print(f"⚠️ Erreur parsing date created_at : {e}")

    print(f"📊 Statut actuel trouvé dans Supabase : {statut_actuel} (Créé à timestamp: {date_creation_utc})")

    # =========================================================================
    # 1. SI STATUT EST "en_attente"
    # =========================================================================
    if statut_actuel == "en_attente":
        reponse = verifier_reponse_telegram_securisee(date_creation_utc)
        print(f"💬 Réponse Telegram détectée : '{reponse}'")

        if reponse == "oui":
            if verifier_plage_horaire():
                print("🚀 'oui' valide + DANS la plage horaire ! Publication immédiate...")
                supabase.table("historique_publications").update({"statut": "valide"}).eq("id", derniere_action["id"]).execute()
                if executer_publication_facebook_et_instagram(derniere_action):
                    texte_nouveau, image_nouveau, _ = generer_nouveau_contenu()
                    asyncio.run(envoyer_vers_telegram(texte_nouveau, image_nouveau))
            else:
                print("⏳ 'oui' valide mais HORS plage horaire ! Passage à 'valide' en attente de l'heure.")
                supabase.table("historique_publications").update({"statut": "valide"}).eq("id", derniere_action["id"]).execute()

        elif reponse == "non":
            print("❌ 'non' valide reçu ! Passage à 'annule', nettoyage du Storage et relance d'un nouveau cycle...")
            supabase.table("historique_publications").update({"statut": "annule"}).eq("id", derniere_action["id"]).execute()
            nettoyer_image_storage(derniere_action.get("image_path"))
            texte_nouveau, image_nouveau, _ = generer_nouveau_contenu()
            asyncio.run(envoyer_vers_telegram(texte_nouveau, image_nouveau))

        else:
            print("🛑 Aucune réponse récente détectée sur Telegram. Le script s'arrête gentiment sans rien toucher.")
            return

    # =========================================================================
    # 2. SI STATUT EST "valide"
    # =========================================================================
    elif statut_actuel == "valide":
        print("🔍 Post déjà validé. Vérification directe de la plage horaire...")
        if verifier_plage_horaire():
            print("🚀 Dans la plage horaire ! Publication et relance d'un nouveau cycle...")
            if executer_publication_facebook_et_instagram(derniere_action):
                texte_nouveau, image_nouveau, _ = generer_nouveau_contenu()
                asyncio.run(envoyer_vers_telegram(texte_nouveau, image_nouveau))
        else:
            print("⏸️ Hors de la plage horaire. Le script s'arrête en conservant le statut 'valide'.")
            return

    # =========================================================================
    # 3. SI STATUT EST "annule" OU "termine"
    # =========================================================================
    elif statut_actuel in ["annule", "termine"]:
        print("ℹ️ La dernière publication est déjà terminée ou annulée. Rien à publier.")
        return

    print("\n✅ Script exécuté avec succès et terminé !")


if __name__ == "__main__":
    main()
