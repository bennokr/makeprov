from __future__ import annotations

import csv

from rdflib import Graph, Literal, Namespace
from rdflib.namespace import RDF, XSD

from makeprov import (
    CachedDownload,
    InPath,
    OutPath,
    ProvenanceConfig,
    main,
    rule,
    span,
)

# Configure a dedicated provenance directory for this workflow example.
ProvenanceConfig.set(
    ProvenanceConfig.get().clone_with(
        prov_dir="sales_prov",
        base_iri="http://example.org/sales/",
        out_fmt="trig",
    )
)

SALES = Namespace("http://example.org/sales/")


@rule()
def create_seed_data(
    products_csv: OutPath = OutPath("data/products.csv"),
    orders_csv: OutPath = OutPath("data/orders.csv"),
) -> None:
    """Write example product and order CSV files used by later steps."""

    products = [
        {"product_id": "P-001", "name": "Widget", "unit_price": "19.99"},
        {"product_id": "P-002", "name": "Gadget", "unit_price": "29.99"},
        {"product_id": "P-003", "name": "Doohickey", "unit_price": "9.99"},
    ]

    with products_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=products[0].keys())
        writer.writeheader()
        writer.writerows(products)

    orders = [
        {"order_id": "O-100", "product_id": "P-001", "region": "North", "units": "4"},
        {"order_id": "O-101", "product_id": "P-002", "region": "South", "units": "3"},
        {"order_id": "O-102", "product_id": "P-001", "region": "North", "units": "2"},
        {"order_id": "O-103", "product_id": "P-003", "region": "East", "units": "5"},
        {"order_id": "O-104", "product_id": "P-002", "region": "West", "units": "1"},
        {"order_id": "O-105", "product_id": "P-003", "region": "South", "units": "7"},
    ]

    with orders_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=orders[0].keys())
        writer.writeheader()
        writer.writerows(orders)


@rule()
def build_region_totals(
    products_csv: InPath = InPath("data/products.csv"),
    orders_csv: InPath = InPath("data/orders.csv"),
    totals_csv: OutPath = OutPath("data/region_totals.csv"),
) -> None:
    """Combine the product and order data into a per-region revenue report."""
    prices: dict[str, float] = {}
    with products_csv.open("r", newline="") as handle:
        for row in csv.DictReader(handle):
            prices[row["product_id"]] = float(row["unit_price"])

    totals: dict[str, dict[str, float]] = {}
    with orders_csv.open("r", newline="") as handle:
        for row in csv.DictReader(handle):
            region = row["region"]
            product_id = row["product_id"]
            units = float(row["units"])
            revenue = prices[product_id] * units

            entry = totals.setdefault(region, {"units": 0.0, "revenue": 0.0})
            entry["units"] += units
            entry["revenue"] += revenue

    with totals_csv.open("w", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["region", "total_units", "total_revenue"],
        )
        writer.writeheader()
        for region, summary in sorted(totals.items()):
            writer.writerow(
                {
                    "region": region,
                    "total_units": f"{summary['units']:.0f}",
                    "total_revenue": f"{summary['revenue']:.2f}",
                }
            )


@rule()
def export_totals_graph(
    totals_csv: InPath = InPath("data/region_totals.csv"),
    graph_ttl: OutPath = OutPath("data/region_totals.ttl"),
) -> Graph:
    """Convert the per-region totals into an RDF graph and serialize it."""
    graph = Graph()
    graph.bind("sales", SALES)

    with totals_csv.open("r", newline="") as handle:
        for row in csv.DictReader(handle):
            region_key = row["region"].lower().replace(" ", "-")
            subject = SALES[f"region/{region_key}"]

            graph.add((subject, RDF.type, SALES.RegionTotal))
            graph.add((subject, SALES.regionName, Literal(row["region"])))
            graph.add(
                (
                    subject,
                    SALES.totalUnits,
                    Literal(row["total_units"], datatype=XSD.integer),
                )
            )
            graph.add(
                (
                    subject,
                    SALES.totalRevenue,
                    Literal(row["total_revenue"], datatype=XSD.decimal),
                )
            )

    with graph_ttl.open("w") as handle:
        handle.write(graph.serialize(format="turtle"))

    # Returning the graph lets makeprov embed the data as a named graph in the
    # provenance dataset.
    return graph


@rule(phony=True)
def fetch_reference_rates(
    rates_json: CachedDownload = CachedDownload(
        "https://example.org/rates.json", "data/rates.json"
    ),
):
    """Demonstrate cached downloads that track source URLs in provenance."""

    with rates_json.open() as handle:
        # In a real workflow you might parse these rates; here we just ensure
        # the cache is populated when invoked.
        return handle.read()


@rule(phony=True)
def build_sales_report() -> Graph:
    """Run the entire workflow and return the final RDF graph."""
    products_csv: OutPath = OutPath("data/products.csv"),
    orders_csv: OutPath = OutPath("data/orders.csv"),
    totals_csv: OutPath = OutPath("data/region_totals.csv"),
    graph_ttl: OutPath = OutPath("data/region_totals.ttl"),

    with span("sales-report", prov_path="sales_prov/sales-report") as sp:
        # Optional: warm a cached download with provenance linkage
        # fetch_reference_rates()
        create_seed_data(products_csv=products_csv, orders_csv=orders_csv)
        build_region_totals(
            products_csv=products_csv.as_inpath(),
            orders_csv=orders_csv.as_inpath(),
            totals_csv=totals_csv,
        )
        graph = export_totals_graph(totals_csv=totals_csv.as_inpath(), graph_ttl=graph_ttl)

    # The span returns the merged provenance for nested consumers.
    assert sp.prov is not None and sp.prov.name == "sales-report"
    return graph


if __name__ == "__main__":
    main()
