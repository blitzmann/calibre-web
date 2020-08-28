//*************
// Localstorage keys for various things
//
// The auth token. Figured it might be nice to be able to save the auth token to the user's browser so that they don't
// always need to go to HB I'm not sure how long this actually lasts for, but I've tested it being active for multiple days.
const HB_AUTH_TOKEN = "hb_auth_token"
//
// The task that is running the bundle fetching is saved here, so that we can return to the proper progress if the page
// is refreshed
const HB_TASK_ID = "hb_get_orders_task_id"
//
// Results of the humble bundle stuff are cached so that we don't need to re-fetch
const HB_RESULTS = "hb_results"
//
// ************

// This is the array that will store the bundle information that we get back form the server
let results = []

$('#hb-progress').hide();
$("#bundle_results").hide();
$("#error_message").hide();

let token = localStorage.getItem(HB_AUTH_TOKEN)
if (token) {
    $("#humble_auth input[name=auth_token]").val(token);
}

let task_id = localStorage.getItem(HB_TASK_ID)
// If we have a task ID saved, start doing status checks
if (task_id) {
    // TODO: this will break if the server dies... give each task a UUID and send that with the task ID (or maybe just use that as what we ask for)
    // TODO: if the task no longer exists on the server, skip the checking
    setTimeout(doStatusCheck(parseInt(task_id)), 1000);
}

let results_str = localStorage.getItem(HB_RESULTS)
if (results_str) {
    // we have cached bnundle information, use it.
    // todo: invalidate this cache if over 12 hours (the TTL for the download links are 24 hours but if we keep this
    // data around right up until then, we don't give us enough time to actually download them)
    results = JSON.parse(results_str)
    processResults(results);
}

// Process the raw bundle data from the server into DOM elements
function processResults(newResults) {
    results = newResults;
    $("#bundle_list").empty()
    for (let bundle of results) {
        bundleIdx = results.indexOf(bundle)
        let innerHtml = "";
        for (let product of bundle.products) {
            productIdx = bundle.products.indexOf(product)
            let totalSize = bundle.products.reduce((a, b) => a + b.file_size, 0);
            innerHtml += `
                <div class="book-row" data-size="${totalSize}">
                    <span class="book-title">${product.name}</span>`;
            for (let dl of product.downloads) {
                dlIdx = product.downloads.indexOf(dl)
                innerHtml += `
                    <button class="btn btn-default book-format format-on" data-selected="true" data-size="${dl.file_size}" data-path="${bundleIdx}.${productIdx}.${dlIdx}">${dl.name}<br /><small>${dl.human_size}</small></span>`;
            }
            innerHtml += `
                </div>`;
        }

        let html = `
            <div class="panel panel-default">
            <div class="panel-heading" role="tab" id="heading-${bundleIdx}">
            <h4 class="panel-title">
                <a role="button" data-toggle="collapse" href="#collapse-${bundleIdx}" aria-expanded="true" aria-controls="collapse-${bundleIdx}">
                    <input type="checkbox" checked class="bundle-toggle" data-path="${bundleIdx}" /> ${bundle.name}
                </a>
                </h4>
            </div>
            <div id="collapse-${bundleIdx}" class="panel-collapse collapse" role="tabpanel" aria-labelledby="heading-${bundleIdx}">
                <div class="panel-body">
                    ${innerHtml}
                </div>
                </div>
            </div>
        `

        $("#bundle_list").append(html);
        $("#bundle_results").show();

        // Create click event for the bundle selection
        $('.bundle-toggle').unbind().click(function(event){
            event.stopPropagation();
            let a = $(this).data('path');
            let obj = results[a];
            obj._ignore = !this.checked
            console.log(obj._ignore)
        })

        // create a click event for per-format downloads
        $('.book-row > .book-format').unbind().click(function(event) {
            let selected = $(this).data('selected');
            // I hate this, but this is the best way that I can think of sans two-way data-binding. The path to the
            // download is saved on each DOM element, and is saved as the index of [bundle].[product].[download]
            [a, b, c] = $(this).data('path').split('.');
            let obj = results[a].products[b].downloads[c];

            if (!selected) {
                // turning it on
                $(this).addClass('format-on')
                obj._ignore = false;
            } else {
                // turning it off
                $(this).removeClass('format-on')
                obj._ignore = true;
            }

            $(this).data('selected', !selected);
        })

    }

}

// Remove elements from an array using a callback
function removePredicate(arr, callback) {
    var i = arr.length;
    while (i--) {
        if (callback(arr[i], i)) {
            arr.splice(i, 1);
        }
    }
};

function submitBundles() {
    removePredicate(results, x=>x._ignore)
    for (let bundle of results) {
        for (let product of bundle.products) {
            removePredicate(product.downloads, x=>x._ignore)
        }
    }

    $.ajax({
        method: "POST",
        data: JSON.stringify(results),
        contentType: "application/json",
        url: HB_BUNDLE_SUBMIT,
        success: function success(data) {
            // we have successfully started a bundle download, clear out all our cached data
            localStorage.removeItem(HB_TASK_ID)
            //localStorage.removeItem(HB_RESULTS)
            $("#bundle_list").empty()
        },
        error: function() {
            // todo: show error on the page
        }
    });

}

function updateProgressBar(percent){
    // otherwise, update progress and keep checking
    let el = $('#hb-progress > .progress-bar');
    el.attr("aria-valuenow", percent);
    el.text(percent + " %");
    el.css("width", percent + "%");
}

// WTB SocketIO :(
// Checks the status of the bundle-fetching task. If it's still in progress, calls itself after 1 second, otherwise
// handles the results
function doStatusCheck(task_id) {
    $("#humble_auth :input").prop("disabled", true); // disable the auth token form if we're asking for status

    $('#hb-progress').show(); // always show the progress bar if we're asking for task status
    localStorage.setItem(HB_TASK_ID, task_id)

    return function () {
        $.ajax({
            method: "POST",
            data: JSON.stringify({
                task_id: task_id
            }),
            contentType: "application/json",
            url: HB_TASK_STATUS_API,
            success: function success(data) {
                localStorage.removeItem(HB_TASK_ID)
                if (data.status == "Finished") {
                    // todo: link this to a task
                    localStorage.setItem(HB_RESULTS, JSON.stringify(data.results))
                    $("#humble_auth :input").prop("disabled", null);
                    $('#hb-progress').hide();
                    processResults(data.results);
                    $("#bundle_list").show();

                    localStorage.removeItem(HB_TASK_ID)
                    return
                }
                else if (data.status == "Failed") {
                    $("#humble_auth :input").prop("disabled", null);

                    $('#hb-progress .progress-bar').addClass("progress-bar-danger");
                    localStorage.removeItem(HB_TASK_ID)

                    $("#error_message").html("There was an error processing your orders. Please try again, or contact the administrator.")
                    return $("#error_message").show();
                } else {
                    if (data.status != "Started") {

                        $("#hb-progress .status").html(data.status);
                        $("#hb-progress .status").show();

                    } else {
                        $("#hb-progress .status").html("");
                    }
                }

                // otherwise, update progress and keep checking
                updateProgressBar(parseInt(data.progress*100))

                setTimeout(doStatusCheck(task_id), 1000);
            },
            error: function (data) {
                // make sure that if this call fails, we re-enable the form
                $("#humble_auth :input").prop("disabled", null);
            }
        });
    }
}

// set up handling for submitting the auth token
$("#humble_auth").submit(function (event) {
    event.preventDefault();
    $("#error_message").hide() // clears any previous error
    let token = $("#humble_auth input[name=auth_token]").val();

    // remove previous results if we have any
    localStorage.removeItem(HB_RESULTS);
    $("#bundle_list").hide();
    $("#bundle_results").hide();

    $.ajax({
        method: "POST",
        data: JSON.stringify({
            auth: token
        }),
        contentType: "application/json",
        url: HB_ORDERS_API,
        success: function success(data) {
            if (!data.success) {
                $("#error_message").html(data.error)
                return $("#error_message").show();
            }

            // todo: only do this if a checkbox is checked that says to save it
            localStorage.setItem(HB_AUTH_TOKEN, token)

            $('#hb-progress .progress-bar').removeClass("progress-bar-danger");
            updateProgressBar(0)
            setTimeout(doStatusCheck(data.task_id), 1000);
        },
        error: function() {
            $("#error_message").html("There was an unknown error fetching your orders.")
            $("#error_message").show();
        }
    });
});
