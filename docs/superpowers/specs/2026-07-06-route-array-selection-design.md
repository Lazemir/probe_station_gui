# Route Array From Selection Design

## Goal

The route planner must let an operator duplicate any selected route-point structure as an array. A selected set of route points is treated as the first array cell, so the original selected points remain in place and new route points are added for the remaining cells.

## User Behavior

- The existing Array tool continues to create a grid from one origin point when there is no multi-point selection.
- The route table supports selecting multiple route rows.
- If the Array tool is used with two or more selected route points, the selected points become the template for cell `[0,0]`.
- Creating the array copies the selected template into every other cell produced by Dir 1, Count 1, Dir 2, Count 2, and Serpentine.
- The original selected points are not deleted or moved.
- Within each copied cell, point order follows the selected rows in route order.
- Serpentine changes the order of array cells, not the order of points inside each copied cell.
- Replace remains available for the existing single-origin grid path. For selection arrays, Replace is disabled or ignored because the approved behavior keeps the source points.

## Architecture

`MeasurementRoute` owns the geometric copy behavior. Add a route-model method that receives selected point indices, two step vectors, two counts, and serpentine mode. The method validates counts and non-zero vectors the same way `add_grid_points` does, copies source point geometry into every generated cell except `[0,0]`, and assigns fresh route point ids and labels.

`DesignNavigatorPanel` owns selection and preview. The route table changes from single-row selection to multi-row selection. The panel keeps the first selected row as the active route point for existing Measure, Move, Remove, and route navigation actions. It also exposes the full selected row list when emitting the Array request.

`Main._add_route_array_points` remains the application boundary. It decides whether to call the existing single-origin grid method or the new selection-copy method. Measurement runner behavior is untouched.

## Data Flow

1. Operator selects route rows in the route table.
2. Operator opens Array, sets vectors/counts, and clicks Create.
3. Panel emits the existing array parameters plus selected route row indices.
4. Main creates a route if needed, then calls:
   - `add_grid_points` for zero/one selected row.
   - `add_array_copies_from_points` for two or more selected rows.
5. Main refreshes design panels and selects the last added point.

## Errors

- Invalid counts raise `RouteModelError`.
- A repeated direction with a zero vector raises `RouteModelError`.
- Selection indices outside the current route raise `RouteModelError`.
- A selection array with fewer than two selected points falls back to the existing single-origin behavior.

## Tests

- Route model test: two selected source points copied into a 2 x 2 array, preserving source points and adding only non-origin-cell copies.
- Route model test: serpentine changes copied cell order while preserving in-cell point order.
- Route model test: invalid selected index raises `RouteModelError`.
- UI or controller-level test: multiple selected rows are passed to array creation while the first selected row remains the active route point.

