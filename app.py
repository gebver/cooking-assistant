import streamlit as st
import re
from openai import OpenAI
from functools import lru_cache

from utils import (
    load_json,
    build_ingredient_lookup,
    calculate_calories,
    get_available_recipes,
    get_missing_ingredients
)
from vector_store import init_vector_store, query_similar_recipes

# --- Configuration & Secrets ---
st.set_page_config(
    page_title="Kuchenny RAG 2.0", 
    page_icon="🍽️", 
    layout="wide"
)
API_KEY = st.secrets.get("OPENROUTER_API_KEY")
if not API_KEY:
    st.error("Brak klucza API. Dodaj OPENROUTER_API_KEY do sekcji secrets.toml.")

# --- Load and cache data ---
recipes = load_json("data/recipes.json")
ingredients_data = load_json("data/ingredients.json")
ing_lookup, all_ingredients = build_ingredient_lookup(ingredients_data)

# --- Initialize vector store ---
embed_model, embed_index = init_vector_store(recipes)

# --- OpenAI chat helper ---
client = OpenAI(api_key=API_KEY, base_url="https://openrouter.ai/api/v1")

def ai_chat(messages: list) -> str:
    try:
        resp = client.chat.completions.create(
            model="mistralai/devstral-small:free",
            messages=messages
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"Błąd API: {e}")
        return "[błąd generowania]"

@lru_cache(maxsize=64)
def generate_instructions(title: str, ingredients: str) -> str:
    prompt = [
        {"role": "system", "content": "Jesteś asystentem kulinarnym podającym zwięzłe instrukcje."},
        {"role": "user", "content": (
            f"Przepis: '{title}'. Składniki: {ingredients}. "
            "Podaj krótką instrukcję przygotowania krok po kroku w punktach."
        )}
    ]
    return ai_chat(prompt)

# --- UI Layout ---
st.title("🍲 Twój kuchenny asystent AI 2.0")
recipes_tab, chat_tab = st.tabs(["🔍 Przepisy", "💬 Chat"])

# --- Przepisy Tab (bez zmian) ---
with recipes_tab:
    # Sidebar options
    st.sidebar.header("Ustawienia przepisów")
    diet = st.sidebar.selectbox(
        "Wybierz dietę", 
        ["dowolna", "wege", "keto", "niskotłuszczowa", "niskocukrowa"]
    )
    
    max_kcal = st.sidebar.slider(
            "Maksymalna liczba kalorii",
            min_value=0,
            max_value=1000,
            value=500,
            step=10
        )

    have = st.sidebar.multiselect(
        "Składniki, które masz", 
        options=all_ingredients
    )
    if st.sidebar.button("Szukaj przepisów"):
        if not have:
            st.sidebar.warning("Wybierz przynajmniej jeden składnik.")
        else:
            exact = get_available_recipes(have, recipes, diet)
            if exact:
                st.subheader("Przepisy pasujące do Twoich składników")
                seen = set()
                for r in exact:
                    title = re.sub(r"\s+\d+$", "", r['title'])
                    if title in seen:
                        continue
                    seen.add(title)
                    kcal = calculate_calories(r['ingredients'], ing_lookup)
                    with st.expander(f"{title} — {r['category']} ({kcal} kcal)"):
                        st.write(f"**Składniki:** {r['ingredients']}")
                        instr = generate_instructions(title, r['ingredients'])
                        st.markdown(instr)
            else:
                st.warning("Brak dokładnych dopasowań.")

            partial = get_missing_ingredients(have, recipes, diet)
            if partial:
                st.subheader("Przepisy z brakującymi składnikami")
                seen = set()
                for r, missing in partial:
                    title = re.sub(r"\s+\d+$", "", r['title'])
                    if title in seen:
                        continue
                    seen.add(title)
                    kcal = calculate_calories(r['ingredients'], ing_lookup)
                    with st.expander(f"{title} — {r['category']} ({kcal} kcal)"):
                        st.write(f"**Składniki:** {r['ingredients']}")
                        st.write(f"**Brakuje:** {', '.join(missing)}")
                        instr = generate_instructions(title, r['ingredients'])
                        st.markdown(instr)

            st.divider()
            st.subheader("Sugestie AI")
            sims = query_similar_recipes(
                query=", ".join(have),
                recipes=recipes,
                model=embed_model,
                index=embed_index,
                top_k=10
            )
            seen_cat = set()
            count = 0
            for r in sims:
                cat = r['category']
                if cat in seen_cat:
                    continue
                seen_cat.add(cat)
                title = re.sub(r"\s+\d+$", "", r['title'])
                kcal = calculate_calories(r['ingredients'], ing_lookup)
                with st.expander(f"{title} — {cat} ({kcal} kcal)"):
                    st.write(f"**Składniki:** {r['ingredients']}")
                    instr = generate_instructions(title, r['ingredients'])
                    st.markdown(instr)
                count += 1
                if count >= 3:
                    break

# --- Chat Tab z RAG ---
with chat_tab:
    if 'chat_history' not in st.session_state:
        st.session_state.chat_history = [
            {"role": "system", "content": "Jesteś pomocnym kuchennym asystentem AI."}
        ]
    # Wyświetl historię czatu
    for msg in st.session_state.chat_history[1:]:
        with st.chat_message(msg['role']):
            st.markdown(msg['content'])
    
    # Wejście użytkownika
    user_msg = st.chat_input("Napisz do asystenta...")

    if user_msg:
        st.chat_message("user").markdown(user_msg)
        st.session_state.chat_history.append({"role": "user", "content": user_msg})

        # --- RAG: pobierz podobne przepisy ---
        similar_recipes = query_similar_recipes(
            query=user_msg,
            recipes=recipes,
            model=embed_model,
            index=embed_index,
            top_k=3
        )
        # Zbuduj kontekst na podstawie podobnych przepisów
        context_texts = []
        for r in similar_recipes:
            context_texts.append(f"Przepis: {r['title']}\nSkładniki: {r['ingredients']}\nOpis: {r.get('description', '')}")

        context_str = "\n\n".join(context_texts) if context_texts else "Brak podobnych przepisów."

        # Stwórz prompt dla modelu z kontekstem
        messages = [
            {"role": "system", "content": "Jesteś pomocnym kuchennym asystentem AI. Odpowiadasz po polsku." 
                                          "Udzielaj odpowiedzi bazując na dostępnych przepisach:"},
            {"role": "system", "content": context_str},
            *st.session_state.chat_history[1:],  # dodaj historię od użytkownika
            {"role": "user", "content": user_msg}
        ]

        # Wywołaj model z rozszerzonym kontekstem
        reply = ai_chat(messages)

        st.session_state.chat_history.append({"role": "assistant", "content": reply})
        st.chat_message("assistant").markdown(reply)