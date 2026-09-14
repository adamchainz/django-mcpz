from __future__ import annotations

import datetime as dt

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from pizzeria.models import Pizza


def menu(request: HttpRequest, on_date: dt.date) -> HttpResponse:
    """The menu for a date, past or future, as a plain web page."""
    pizzas = [pizza for pizza in Pizza.objects.all() if pizza.available_on(on_date)]
    return render(request, "pizzeria/menu.html", {"on_date": on_date, "pizzas": pizzas})
