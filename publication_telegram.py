import asyncio
import base64
import io
import json
import os
import random
import time
import uuid
from datetime import datetime
from urllib.parse import quote

import requests
from PIL import Image, ImageOps
from supabase import Client, create_client
from telegram import Bot
from telegram.request import HTTPXRequest

# ============================================================
# CONFIGURATION
# ============================================================
SUPABASE_URL_DEFAUT = "https://acdqyhafpzrlrsheuxgq.supabase.co"
SUPABASE_KEY_DEFAUT = "sb_publishable_ZbIRDJWZ1uGxV4ZsvsFxKg_0SoG4TOU"

PROMPTIX_URL = "https://promptix.tn/api/v1/chat/completions"
MODELE_TEXTE = "google/gemini-3.8-flash"
MODELE_IMAGE = "google/gemini-3.1-flash-image-preview"
GRAPH_API_VERSION = "v26.0"
BUCKET_NAME = "patisseries"
FICHIER_RESULTAT = "resultat.png"

# IMPORTANT : mettez ici le nom EXACT du logo PNG transparent dans le bucket.
# Exemple : "logo_transparent.png" ou "logo.png".
LOGO_STORAGE_PATH = "saveurs_dammak_logo_transparent.png"
GLOVO_STORAGE_PATH = "glovo.jpg"
ZIGZAG_STORAGE_PATH = "zigzag.jpg"

TIMEOUT_STANDARD = 60
MAX_TENTATIVES_GENERATION = 3

session = requests.Session()


def lire_table_supabase(client: Client, nom_table: str, colonne: str):
    try:
        res = client.table(nom_table).select(colonne).limit(1).execute()
        if res.data and res.data[0].get(colonne) is not None:
            valeur = str(res.data[0][colonne]).strip().strip('"').strip("'")
            return valeur or None
    except Exception as exc:
        print(f"⚠️ Lecture {nom_table}.{colonne} impossible : {exc}")
    return None


def initialiser_supabase():
    client_initial = create_client(SUPABASE_URL_DEFAUT, SUPABASE_KEY_DEFAUT)
    url = lire_table_supabase(client_initial, "table_url", "valeur") or SUPABASE_URL_DEFAUT
    cle = lire_table_supabase(client_initial, "table_key", "valeur") or SUPABASE_KEY_DEFAUT
    client = create_client(url, cle)
    return client, url, cle


supabase, SUPABASE_URL, SUPABASE_KEY = initialiser_supabase()
PROMPTIX_KEY = lire_table_supabase(supabase, "table_cle", "valeur")
PAGE_ACCESS_TOKEN = lire_table_supabase(supabase, "table_token_facebook", "valeur")
TELEGRAM_BOT_TOKEN = lire_table_supabase(supabase, "table_telegram_token", "valeur")
TELEGRAM_CHAT_ID = lire_table_supabase(supabase, "table_telegram_chat_id", "valeur")
PAGE_ID = (
    lire_table_supabase(supabase, "table_page_id", "valeur")
    or lire_table_supabase(supabase, "table_page_id", "page_id")
    or "111157353629841"
)
INSTAGRAM_BUSINESS_ID = (
    lire_table_supabase(supabase, "instagram_config", "instagram_business_id")
    or lire_table_supabase(supabase, "instagram_config", "valeur")
    or "17841451463230647"
)

BASE_GRAPH_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
GRAPH_URL_FB = f"{BASE_GRAPH_URL}/{PAGE_ID}/photos"


def verifier_configuration():
    manquants = []
    for nom, valeur in [
        ("PROMPTIX_KEY/table_cle", PROMPTIX_KEY),
        ("PAGE_ACCESS_TOKEN/table_token_facebook", PAGE_ACCESS_TOKEN),
        ("TELEGRAM_BOT_TOKEN/table_telegram_token", TELEGRAM_BOT_TOKEN),
        ("TELEGRAM_CHAT_ID/table_telegram_chat_id", TELEGRAM_CHAT_ID),
    ]:
        if not valeur:
            manquants.append(nom)
    if manquants:
        raise RuntimeError("Configuration manquante dans Supabase : " + ", ".join(manquants))


def recuperer_restriction_supabase(nom_table):
    try:
        res = supabase.table(nom_table).select("contenu").limit(1).execute()
        if res.data:
            return str(res.data[0].get("contenu") or "").strip()
    except Exception as exc:
        print(f"⚠️ Restriction {nom_table} indisponible : {exc}")
    return ""


def verifier_plage_horaire():
    try:
        res = supabase.table("table_horaires_publication").select("*").limit(1).execute()
        if not res.data:
            return True
        ligne = res.data[0]
        debut_txt = str(ligne.get("heure_debut", "00:00:00"))
        fin_txt = str(ligne.get("heure_fin", "23:59:59"))
        debut = datetime.strptime(debut_txt, "%H:%M:%S").time()
        fin = datetime.strptime(fin_txt, "%H:%M:%S").time()
        maintenant = datetime.now().time()
        # Gère aussi une plage qui traverse minuit.
        dans_plage = debut <= maintenant <= fin if debut <= fin else (maintenant >= debut or maintenant <= fin)
        print(f"⏰ Plage : {debut}–{fin} | Maintenant : {maintenant.strftime('%H:%M:%S')} | Autorisée : {dans_plage}")
        return dans_plage
    except Exception as exc:
        print(f"⚠️ Vérification horaire impossible : {exc}")
        return True


def verifier_reponse_telegram_securisee(date_creation_post_utc):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return ""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        reponse = session.get(url, timeout=30)
        reponse.raise_for_status()
        contenu = reponse.json()
        if not contenu.get("ok"):
            return ""
        for item in reversed(contenu.get("result", [])):
            message = item.get("message") or item.get("edited_message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if chat_id != str(TELEGRAM_CHAT_ID):
                continue
            timestamp = message.get("date")
            texte = str(message.get("text") or "").strip().lower()
            if not timestamp or not date_creation_post_utc:
                continue
            if timestamp <= date_creation_post_utc:
                # Les mises à jour Telegram sont chronologiques ; on ignore les anciens messages.
                continue
            if texte in ("oui", "valider"):
                return "oui"
            if texte in ("non", "refuser"):
                return "non"
        return ""
    except Exception as exc:
        print(f"⚠️ Lecture Telegram impossible : {exc}")
        return ""


def telecharger_fichier_storage(nom_fichier):
    chemin = str(nom_fichier or "").lstrip("/")
    url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{quote(chemin, safe='/')}"
    try:
        reponse = session.get(url, timeout=TIMEOUT_STANDARD)
        if reponse.status_code == 200:
            return reponse.content
        print(f"⚠️ Storage {chemin}: HTTP {reponse.status_code}")
    except Exception as exc:
        print(f"⚠️ Téléchargement {chemin} impossible : {exc}")
    return None


def telecharger_image_supabase(nom_fichier, conserver_alpha=False):
    contenu = telecharger_fichier_storage(nom_fichier)
    if not contenu:
        return None
    try:
        image = Image.open(io.BytesIO(contenu))
        image.load()
        return image.convert("RGBA" if conserver_alpha else "RGB")
    except Exception as exc:
        print(f"⚠️ Image {nom_fichier} illisible : {exc}")
        return None


def photo_en_base64(img, taille_max=1024, format_image="JPEG"):
    copie = img.copy()
    copie.thumbnail((taille_max, taille_max), Image.Resampling.LANCZOS)
    tampon = io.BytesIO()
    if format_image.upper() == "PNG":
        copie.save(tampon, format="PNG", optimize=True)
    else:
        if copie.mode not in ("RGB", "L"):
            fond = Image.new("RGB", copie.size, "white")
            if copie.mode == "RGBA":
                fond.paste(copie, mask=copie.getchannel("A"))
            else:
                fond.paste(copie.convert("RGB"))
            copie = fond
        copie.save(tampon, format="JPEG", quality=92, optimize=True)
    return base64.b64encode(tampon.getvalue()).decode("ascii")


def appeler_promptix(payload):
    if not PROMPTIX_KEY:
        raise RuntimeError("Clé Promptix absente (table_cle).")
    return session.post(
        PROMPTIX_URL,
        headers={"Authorization": f"Bearer {PROMPTIX_KEY}"},
        json=payload,
        timeout=300,
    )


def extraire_image(message):
    for element in message.get("images") or []:
        url = (element.get("image_url") or {}).get("url", "")
        try:
            if url.startswith("data:image/"):
                return base64.b64decode(url.split(",", 1)[1])
            if url.startswith("http://") or url.startswith("https://"):
                rep = session.get(url, timeout=120)
                rep.raise_for_status()
                return rep.content
        except Exception as exc:
            print(f"⚠️ Récupération de l'image générée impossible : {exc}")
    # Certains fournisseurs renvoient l'image dans le contenu texte.
    contenu = message.get("content")
    if isinstance(contenu, list):
        for bloc in contenu:
            if isinstance(bloc, dict):
                url = (bloc.get("image_url") or {}).get("url", "")
                if url.startswith("data:image/"):
                    return base64.b64decode(url.split(",", 1)[1])
    return None


def nettoyer_image_storage(image_path):
    if not image_path:
        return
    try:
        supabase.storage.from_(BUCKET_NAME).remove([image_path])
        print(f"🧹 Image temporaire supprimée : {image_path}")
    except Exception as exc:
        print(f"⚠️ Nettoyage Storage impossible : {exc}")


def incruster_logo(image_base, logo, position="haut_droite", largeur_relative=0.20, marge_relative=0.035):
    """Incruste un logo PNG déjà transparent, après la génération IA."""
    base = image_base.convert("RGBA")
    if logo is None:
        print("⚠️ Aucun logo chargé : image publiée sans incrustation du logo.")
        return base

    logo = logo.convert("RGBA")
    if logo.getchannel("A").getextrema() == (255, 255):
        print(
            "⚠️ Le logo n'a pas de transparence (alpha entièrement opaque). "
            "Ne pas l'incruster automatiquement pour éviter un carré visible. "
            "Utilise un vrai PNG transparent."
        )
        return base

    largeur = max(1, int(base.width * largeur_relative))
    hauteur = max(1, round(logo.height * largeur / logo.width))
    logo.thumbnail((largeur, hauteur), Image.Resampling.LANCZOS)
    marge = int(min(base.size) * marge_relative)

    if position == "haut_centre":
        x = (base.width - logo.width) // 2
        y = marge
    elif position == "haut_droite":
        x = base.width - logo.width - marge
        y = marge
    else:
        x = base.width - logo.width - marge
        y = base.height - logo.height - marge

    base.alpha_composite(logo, (max(0, x), max(0, y)))
    return base


def televerser_image_temporaire(image, nom_fichier):
    tampon = io.BytesIO()
    image.save(tampon, format="PNG", optimize=True)
    tampon.seek(0)
    supabase.storage.from_(BUCKET_NAME).upload(
        nom_fichier,
        tampon.getvalue(),
        file_options={"content-type": "image/png", "upsert": "true"},
    )


def generer_nouveau_contenu():
    print("\n-----------------------------------------")
    print(f"🔄 Nouveau cycle créatif ({MODELE_TEXTE})")

    articles = supabase.table("articles").select("*").execute().data or []
    if not articles:
        raise RuntimeError("Aucune donnée dans la table 'articles'.")

    sac = []
    for article in articles:
        try:
            poids = max(1, min(int(article.get("importance") or 3), 20))
        except (TypeError, ValueError):
            poids = 3
        sac.extend([article] * poids)
    article = random.choice(sac)
    nom_article = str(article.get("article") or "notre création")
    categorie = str(article.get("categorie") or "Pâtisserie")
    print(f"📌 Produit choisi : {nom_article} ({categorie})")

    ref_photo = str(article.get("ref_photo") or "").strip()
    if not ref_photo:
        raise RuntimeError(f"Aucune ref_photo pour l'article {nom_article}.")

    candidats = [ref_photo] if ref_photo.lower().endswith((".jpg", ".jpeg", ".png", ".webp")) else [
        f"{ref_photo}.jpeg", f"{ref_photo}.jpg", f"{ref_photo}.png", f"{ref_photo}.webp"
    ]
    photo = None
    for chemin in candidats:
        photo = telecharger_image_supabase(chemin)
        if photo is not None:
            print(f"✅ Photo source chargée : {chemin}")
            break
    if photo is None:
        raise RuntimeError(f"Impossible de télécharger la photo de {nom_article}. Vérifie ref_photo et le bucket.")

    print("📥 Chargement du logo et des logos partenaires…")
    logo = telecharger_image_supabase(LOGO_STORAGE_PATH, conserver_alpha=True)
    glovo = telecharger_image_supabase(GLOVO_STORAGE_PATH, conserver_alpha=True)
    zigzag = telecharger_image_supabase(ZIGZAG_STORAGE_PATH, conserver_alpha=True)
    if logo is None:
        print(f"⚠️ Logo introuvable ({LOGO_STORAGE_PATH}). Vérifie le nom exact dans Storage.")

    restriction_image = recuperer_restriction_supabase("table_restriction_image")
    restriction_texte = recuperer_restriction_supabase("table_restriction_texte")

    prompt_directeur = f"""
Tu es directeur marketing d'une maison de pâtisserie fine à Sfax.
Produit : {nom_article}
Catégorie : {categorie}

Consignes de texte à respecter :
{restriction_texte}

Réponds avec EXACTEMENT ces deux sections :
[DIRECTION_ARTISTIQUE_IMAGE]
Une description visuelle concise, réaliste et réalisable, qui met en valeur le produit réellement présent dans la photo.
[TEXTE_FACEBOOK]
Le texte final prêt à publier. N'invente aucun ingrédient, label, certification, prix, disponibilité ou caractéristique non fourni.
Ne répète pas le même texte générique. Écris en français naturel, élégant et spécifique au produit.
"""
    rep = appeler_promptix({
        "model": MODELE_TEXTE,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_directeur},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(photo)}},
            ],
        }],
    })
    rep.raise_for_status()
    donnees = rep.json()
    contenu = donnees.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not contenu:
        raise RuntimeError(f"Promptix n'a pas renvoyé de texte exploitable : {json.dumps(donnees, ensure_ascii=False)[:1000]}")

    if "[TEXTE_FACEBOOK]" not in contenu:
        raise RuntimeError("Réponse du directeur IA incomplète : balise [TEXTE_FACEBOOK] absente.")
    parties = contenu.split("[TEXTE_FACEBOOK]", 1)
    direction = parties[0].replace("[DIRECTION_ARTISTIQUE_IMAGE]", "").strip()
    texte_facebook = parties[1].strip()

    # Règles minimales côté code pour éviter qu'un modèle oublie les points de vente.
    # Le texte exact reste défini dans la table de restrictions.
    if "RETROUVEZ-NOUS" not in texte_facebook:
        print("⚠️ Le texte IA n'inclut pas RETROUVEZ-NOUS : vérifie la restriction en base.")
    if "#SaveursDammak" not in texte_facebook:
        texte_facebook = texte_facebook.rstrip() + "\n\n#SaveursDammak #PatisserieFine #Sfax #Tunisie"

    # Les logos partenaires sont fournis comme références visuelles seulement.
    # On ne force pas leur affichage : cela évite les logos déformés ou omniprésents.
    contenu_image = [
        {
            "type": "text",
            "text": (
                "Améliore la photo du produit en conservant fidèlement sa forme, sa décoration, "
                "ses couleurs et son identité. Ne change pas le produit en un autre dessert. "
                "Ne dessine aucun texte, aucune étiquette, aucun logo et aucun filigrane. "
                "Le logo officiel sera ajouté ensuite par le programme. "
                "Direction artistique : " + direction + "\nConsignes de composition : " + restriction_image
            ),
        },
        {
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + photo_en_base64(photo)},
        },
    ]
    if glovo:
        contenu_image.extend([
            {"type": "text", "text": "Référence de logo partenaire Glovo uniquement. Ne le redessine pas et ne l'ajoute pas à l'image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + photo_en_base64(glovo, 512, "PNG")}},
        ])
    if zigzag:
        contenu_image.extend([
            {"type": "text", "text": "Référence de logo partenaire Zigzag uniquement. Ne le redessine pas et ne l'ajoute pas à l'image."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + photo_en_base64(zigzag, 512, "PNG")}},
        ])

    rep_image = appeler_promptix({
        "model": MODELE_IMAGE,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": contenu_image}],
    })
    rep_image.raise_for_status()
    donnees_image = rep_image.json()
    message = donnees_image.get("choices", [{}])[0].get("message", {})
    image_bytes = extraire_image(message)
    if not image_bytes:
        raise RuntimeError(f"Aucune image renvoyée par le modèle image : {json.dumps(donnees_image, ensure_ascii=False)[:1200]}")

    with Image.open(io.BytesIO(image_bytes)) as image_generee:
        image_finale = ImageOps.exif_transpose(image_generee).convert("RGBA")

    # Incruster le logo APRÈS l'IA, afin que Gemini ne le redessine pas.
    image_finale = incruster_logo(image_finale, logo, position="haut_droite")
    image_finale.convert("RGB").save(FICHIER_RESULTAT, format="PNG", optimize=True)

    nom_temp = f"temp_{uuid.uuid4().hex[:12]}.png"
    televerser_image_temporaire(image_finale.convert("RGB"), nom_temp)

    insertion = {
        "article_nom": nom_article,
        "categorie": categorie,
        "scenario_titre": "Direction marketing + incrustation logo post-traitement",
        "texte_genere": texte_facebook,
        "image_path": nom_temp,
        "statut": "en_attente",
    }
    resultat_insert = supabase.table("historique_publications").insert(insertion).execute()
    if not resultat_insert.data:
        nettoyer_image_storage(nom_temp)
        raise RuntimeError("La proposition a été créée en image mais l'insertion historique n'a pas confirmé de ligne.")

    print(f"✨ Proposition enregistrée. Image temporaire : {nom_temp}")
    texte_telegram = (
        "<b>🎨 Direction artistique :</b>\n" + direction +
        "\n\n<b>📝 Texte de publication :</b>\n" + texte_facebook
    )
    return texte_telegram, FICHIER_RESULTAT, nom_temp


async def envoyer_vers_telegram(texte, image_path):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise RuntimeError("Configuration Telegram manquante.")
    request = HTTPXRequest(read_timeout=60, write_timeout=60, connect_timeout=60)
    bot = Bot(token=TELEGRAM_BOT_TOKEN, request=request)
    entete = (
        "<b>🍰 Proposition Saveurs Dammak</b>\n\n"
        "<i>Réponds « oui » pour valider et publier, ou « non » pour refuser.</i>\n\n"
    )
    message_complet = entete + texte

    with open(image_path, "rb") as photo:
        if len(message_complet) <= 1024:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo, caption=message_complet, parse_mode="HTML")
        else:
            await bot.send_photo(chat_id=TELEGRAM_CHAT_ID, photo=photo, caption=entete, parse_mode="HTML")
            # Telegram HTML : le texte IA peut contenir <, > ou &, on l'envoie en texte brut.
            texte_brut = message_complet.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "")
            await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=texte_brut)
    print("📱 Proposition envoyée sur Telegram.")


def publier_sur_facebook(texte, image_path):
    if not PAGE_ACCESS_TOKEN:
        return {"error": {"message": "PAGE_ACCESS_TOKEN absent"}}
    with open(image_path, "rb") as fichier:
        rep = session.post(
            GRAPH_URL_FB,
            data={"access_token": PAGE_ACCESS_TOKEN, "message": texte, "published": "true"},
            files={"source": ("resultat.png", fichier, "image/png")},
            timeout=180,
        )
    try:
        resultat = rep.json()
    except ValueError:
        resultat = {"error": {"message": f"Réponse non JSON, HTTP {rep.status_code}", "body": rep.text[:500]}}
    if not rep.ok:
        resultat.setdefault("error", {"message": f"Facebook HTTP {rep.status_code}"})
    return resultat


def publier_sur_instagram(texte, image_url):
    if not PAGE_ACCESS_TOKEN:
        return {"error": {"message": "PAGE_ACCESS_TOKEN absent"}}
    endpoint = f"{BASE_GRAPH_URL}/{INSTAGRAM_BUSINESS_ID}/media"
    try:
        rep = session.post(
            endpoint,
            data={"image_url": image_url, "caption": texte, "access_token": PAGE_ACCESS_TOKEN},
            timeout=120,
        )
        resultat = rep.json()
        if not rep.ok or "id" not in resultat:
            return {"error": resultat}

        creation_id = resultat["id"]
        endpoint_publish = f"{BASE_GRAPH_URL}/{INSTAGRAM_BUSINESS_ID}/media_publish"
        # Vérifie l'état du conteneur au lieu d'attendre arbitrairement 10 secondes.
        for tentative in range(6):
            statut_rep = session.get(
                f"{BASE_GRAPH_URL}/{creation_id}",
                params={"fields": "status_code", "access_token": PAGE_ACCESS_TOKEN},
                timeout=60,
            )
            statut_json = statut_rep.json()
            statut = statut_json.get("status_code")
            if statut == "FINISHED":
                break
            if statut in ("ERROR", "EXPIRED"):
                return {"error": {"message": "Conteneur Instagram non prêt", "details": statut_json}}
            time.sleep(5)
        else:
            return {"error": {"message": "Délai dépassé en attendant le conteneur Instagram"}}

        publication = session.post(
            endpoint_publish,
            data={"creation_id": creation_id, "access_token": PAGE_ACCESS_TOKEN},
            timeout=120,
        )
        resultat_publication = publication.json()
        if not publication.ok or "id" not in resultat_publication:
            return {"error": resultat_publication}
        return resultat_publication
    except Exception as exc:
        return {"error": {"message": str(exc)}}


def executer_publication_facebook_et_instagram(action):
    chemin_image = action.get("image_path")
    if not chemin_image:
        print("❌ image_path absent dans la proposition.")
        return False

    # Télécharger exactement l'image associée à cette ligne, pas une ancienne resultat.png.
    octets = telecharger_fichier_storage(chemin_image)
    if not octets:
        print(f"❌ Image temporaire introuvable dans Storage : {chemin_image}")
        return False
    with Image.open(io.BytesIO(octets)) as image:
        ImageOps.exif_transpose(image).convert("RGB").save(FICHIER_RESULTAT, format="PNG", optimize=True)

    image_publique_url = f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET_NAME}/{quote(chemin_image, safe='/')}"
    texte = str(action.get("texte_genere") or "")

    resultat_fb = publier_sur_facebook(texte, FICHIER_RESULTAT)
    succes_fb = bool(resultat_fb.get("id") or resultat_fb.get("post_id")) and "error" not in resultat_fb
    id_fb = resultat_fb.get("post_id") or resultat_fb.get("id")
    print(("🎉 Facebook publié : " + str(id_fb)) if succes_fb else ("❌ Échec Facebook : " + json.dumps(resultat_fb, ensure_ascii=False)[:1200]))

    resultat_ig = publier_sur_instagram(texte, image_publique_url)
    succes_ig = bool(resultat_ig.get("id")) and "error" not in resultat_ig
    id_ig = resultat_ig.get("id")
    print(("🎉 Instagram publié : " + str(id_ig)) if succes_ig else ("❌ Échec Instagram : " + json.dumps(resultat_ig, ensure_ascii=False)[:1200]))

    if succes_fb or succes_ig:
        supabase.table("historique_publications").update({
            "statut": "termine",
        }).eq("id", action["id"]).execute()

        # Ne supprime l'image qu'après la tentative Instagram, qui doit pouvoir la télécharger.
        nettoyer_image_storage(chemin_image)

        try:
            bot = Bot(token=TELEGRAM_BOT_TOKEN)
            recap = f"Publication terminée.\nFacebook : {'OK' if succes_fb else 'ÉCHEC'}\nInstagram : {'OK' if succes_ig else 'ÉCHEC'}"
            asyncio.run(bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=recap))
        except Exception as exc:
            print(f"⚠️ Envoi du récapitulatif Telegram impossible : {exc}")
        return True

    print("❌ Les deux plateformes ont échoué ; l'image est conservée pour diagnostic.")
    return False


def creer_et_envoyer_proposition():
    texte, image_locale, _ = generer_nouveau_contenu()
    asyncio.run(envoyer_vers_telegram(texte, image_locale))


def main():
    verifier_configuration()
    print("🔍 Recherche de la dernière proposition Supabase…")
    historique = (
        supabase.table("historique_publications")
        .select("*")
        .order("id", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    if not historique:
        print("ℹ️ Aucun historique : création de la première proposition.")
        creer_et_envoyer_proposition()
        return

    action = historique[0]
    statut = str(action.get("statut") or "termine")
    date_creation = None
    created_at = action.get("created_at")
    if created_at:
        try:
            date_creation = int(datetime.fromisoformat(str(created_at).replace("Z", "+00:00")).timestamp())
        except Exception as exc:
            print(f"⚠️ created_at illisible : {exc}")

    print(f"📊 Statut : {statut} | created_at timestamp : {date_creation}")

    if statut == "en_attente":
        reponse = verifier_reponse_telegram_securisee(date_creation)
        print(f"💬 Réponse Telegram : {reponse or '(aucune)'}")
        if reponse == "non":
            supabase.table("historique_publications").update({"statut": "annule"}).eq("id", action["id"]).execute()
            nettoyer_image_storage(action.get("image_path"))
            creer_et_envoyer_proposition()
            return
        if reponse == "oui":
            if not verifier_plage_horaire():
                supabase.table("historique_publications").update({"statut": "valide"}).eq("id", action["id"]).execute()
                print("⏳ Proposition validée ; elle sera publiée quand le script sera relancé pendant la plage autorisée.")
                return
            supabase.table("historique_publications").update({"statut": "valide"}).eq("id", action["id"]).execute()
            action["statut"] = "valide"
            if executer_publication_facebook_et_instagram(action):
                creer_et_envoyer_proposition()
            return
        print("🛑 Aucune réponse récente : aucune action.")
        return

    if statut == "valide":
        if verifier_plage_horaire():
            if executer_publication_facebook_et_instagram(action):
                creer_et_envoyer_proposition()
        else:
            print("⏸️ Hors plage horaire : proposition conservée au statut valide.")
        return

    if statut in ("annule", "termine"):
        print("ℹ️ Dernière proposition déjà annulée ou terminée. Rien à faire.")
        return

    print(f"⚠️ Statut non reconnu : {statut}. Aucune action automatique.")


if __name__ == "__main__":
    main()
