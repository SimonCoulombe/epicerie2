/* Épicerie — Dashboard app logic */

(function () {
    "use strict";

    const productSelect = document.getElementById("product-select");
    const citySelect    = document.getElementById("city-select");
    const chainSelect   = document.getElementById("chain-select");
    const dateFrom      = document.getElementById("date-from");
    const dateTo        = document.getElementById("date-to");
    const btnSearch     = document.getElementById("btn-search");
    const chartDiv      = document.getElementById("price-chart");
    const tableBody     = document.querySelector("#price-table tbody");

    // Default date range: last 30 days
    const today = new Date();
    const thirtyDaysAgo = new Date(today);
    thirtyDaysAgo.setDate(today.getDate() - 30);
    dateTo.value = fmt(today);
    dateFrom.value = fmt(thirtyDaysAgo);

    function fmt(d) {
        return d.toISOString().slice(0, 10);
    }

    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    async function fetchJSON(url) {
        const resp = await fetch(url);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return resp.json();
    }

    function populateSelect(el, items, valueKey, labelKey) {
        el.innerHTML = "";
        items.forEach(function (item) {
            const opt = document.createElement("option");
            opt.value = item[valueKey];
            opt.textContent = item[labelKey];
            opt.selected = true;
            el.appendChild(opt);
        });
    }

    function getSelectedValues(el) {
        return Array.from(el.selectedOptions).map(function (o) { return o.value; });
    }

    // Chart color palette
    var COLORS = ["#2563eb", "#dc2626", "#16a34a", "#f59e0b", "#8b5cf6", "#ec4899"];

    async function loadFilters() {
        const [products, cities, chains] = await Promise.all([
            fetchJSON("/api/products"),
            fetchJSON("/api/cities"),
            fetchJSON("/api/store-chains"),
        ]);
        populateSelect(productSelect, products, "slug", "name");
        populateSelect(citySelect, cities, "slug", "name");
        populateSelect(chainSelect, chains, "name", "name");
    }

    async function search() {
        const params = new URLSearchParams();

        const products = getSelectedValues(productSelect);
        const cities   = getSelectedValues(citySelect);
        const chains   = getSelectedValues(chainSelect);

        // API supports single filter values; fetch per-product if multiple
        if (products.length === 1) params.set("product", products[0]);
        if (cities.length === 1)   params.set("city", cities[0]);
        if (chains.length === 1)   params.set("chain", chains[0]);
        if (dateFrom.value) params.set("from", dateFrom.value);
        if (dateTo.value)   params.set("to", dateTo.value);

        var data;
        if (products.length > 1) {
            // Fetch per product and merge
            var allData = [];
            for (var i = 0; i < products.length; i++) {
                var p = new URLSearchParams(params);
                p.set("product", products[i]);
                var chunk = await fetchJSON("/api/prices?" + p.toString());
                allData = allData.concat(chunk);
            }
            data = allData;
        } else {
            data = await fetchJSON("/api/prices?" + params.toString());
        }

        // Filter client-side for multi-select chains/cities
        if (chains.length > 1) {
            var chainSet = new Set(chains);
            data = data.filter(function (d) { return chainSet.has(d.store_chain); });
        }
        if (cities.length > 1) {
            var citySet = new Set(cities);
            data = data.filter(function (d) { return citySet.has(d.city); });
        }

        renderChart(data);
        renderTable(data);
    }

    function renderChart(data) {
        // Group by store_chain (+ product if multiple)
        var traces = {};
        data.forEach(function (d) {
            var key = d.store_chain;
            if (d.product_name) key = d.product_name + " — " + d.store_chain;
            if (!traces[key]) traces[key] = { x: [], y: [], name: key };
            traces[key].x.push(d.date);
            traces[key].y.push(d.price);
        });

        var plotData = Object.keys(traces).map(function (key, i) {
            var t = traces[key];
            return {
                x: t.x,
                y: t.y,
                name: t.name,
                type: "scatter",
                mode: "lines+markers",
                line: { color: COLORS[i % COLORS.length], width: 2 },
                marker: { size: 6 },
            };
        });

        var layout = {
            title: "Évolution des prix",
            xaxis: { title: "Date", type: "date" },
            yaxis: { title: "Prix ($)", tickprefix: "$", rangemode: "tozero" },
            legend: { orientation: "h", y: -0.2 },
            margin: { t: 50, b: 80, l: 60, r: 20 },
            hovermode: "x unified",
        };

        Plotly.newPlot(chartDiv, plotData, layout, { responsive: true });
    }

    function renderTable(data) {
        tableBody.innerHTML = "";
        if (data.length === 0) {
            tableBody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:#6b7280;">Aucune donnée</td></tr>';
            return;
        }
        data.forEach(function (d) {
            var tr = document.createElement("tr");
            tr.innerHTML =
                "<td>" + escapeHtml(d.date) + "</td>" +
                "<td>" + escapeHtml(d.product_name || "") + "</td>" +
                "<td>" + escapeHtml(d.store_chain) + "</td>" +
                "<td>" + escapeHtml(d.city) + "</td>" +
                "<td>" + (d.price != null ? d.price.toFixed(2) + " $" : "—") + "</td>";
            tableBody.appendChild(tr);
        });
    }

    // Init
    btnSearch.addEventListener("click", search);
    loadFilters().then(search);
})();
