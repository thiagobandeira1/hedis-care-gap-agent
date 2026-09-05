"""Streamlit reviewer console (SPEC section 7).

``client`` wraps every API route with typed returns, ``components`` renders the frozen graph
payloads, ``app`` wires the pages. The UI never computes a verdict: everything on screen is
copied from an API response.
"""
