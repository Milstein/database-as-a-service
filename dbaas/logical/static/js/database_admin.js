(function($) {


    /**
     * setup JQuery's AJAX methods to setup CSRF token in the request before sending it off.
     * http://stackoverflow.com/questions/5100539/django-csrf-check-failing-with-an-ajax-post-request
     */

    function getCookie(name) {
        var cookieValue = null;
        if (document.cookie && document.cookie !== '') {
            var cookies = document.cookie.split(';');
            for (var i = 0; i < cookies.length; i++) {
                var cookie = $.trim(cookies[i]);
                // Does this cookie string begin with the name we want?

                if (cookie.substring(0, name.length + 1) == (name + '=')) {
                    cookieValue = decodeURIComponent(cookie.substring(name.length + 1));
                    break;
                }
            }
        }
        return cookieValue;
    }

    $.ajaxSetup({
         beforeSend: function(xhr, settings) {
             if (!(/^http:.*/.test(settings.url) || /^https:.*/.test(settings.url))) {
                 // Only send the token to relative URLs i.e. locally.
                 xhr.setRequestHeader("X-CSRFToken", getCookie('csrftoken'));
             }
         }
    });

    var CredentialManager = (function() {
        var DATABASE_ID = null;
        var get_database_id = function() {
            if (!DATABASE_ID) {
                DATABASE_ID = $("#table-credentials").data("database-id");
            }
            return DATABASE_ID;
        };

        var Credential = function($row) {
            this.$row = $row;
            this.pk = $row.attr('data-credential-pk');
        };

        /**
        * Reset credential password
        */
        Credential.prototype.reset_password = function(callback) {
            var credential = this;
            if (confirm("Are you sure?")) {
                $.ajax({
                    "url": "/logical/credential/" + this.pk,
                    "type": "PUT",
                }).done(function(data) {
                    $(".show-password", credential.$row).attr("data-content", data.credential.password);
                    if (callback) {
                        callback(credential);
                    }
                });
            }
        };

        /**
        * Remove a credential from server and the page.
        */
        Credential.prototype.delete = function(callback) {
            var credential = this;
            if (confirm("Are you sure?")) {
                $.ajax({
                    "url": "/logical/credential/" + this.pk,
                    "type": "DELETE",
                }).done(function(data) {
                    credential.$row.remove();
                    if (callback) {
                        callback(credential);
                    }
                });
            }
        };

        /**
        * Show credential password. If password is shown, it will be hidden. Use
        * force_show to always show.
        */
        Credential.prototype.show_password = function(force_show) {
            // hide all others passwords
            var credential = this, operation = "toggle";
            if (force_show) operation = "show";

            $(".show-password", "#table-credentials").each(function(i, el) {
                if ($(el).parents(".credential").attr("data-credential-pk") === credential.pk) {
                    $(".show-password", credential.$row).popover(operation);
                } else {
                    $(el).popover("hide");
                }
            });
        };

        /**
        * Put the listeners on credential
        */
        var initialize_listeners = function(credential) {
            // put all listeners
            var $row = credential.$row;

            $(".show-password", $row).popover({"trigger": "manual", "placement": "left"})
            .on('click', function(e) {
                e.preventDefault();
                credential.show_password();
            });

            $row.on("click.reset-password", ".btn-reset-password", function(e) {
                credential.reset_password(function() {
                    credential.show_password(true);
                });
                return false;
            });

            // Delete credential
            $row.on("click.delete-credential", ".btn-credential-remove", function(e) {
                credential.delete();
                return false;
            });
        };

        ///////// ADD BUTTON is the only function isolated
        $(document).on("click.add-credential", "#add-credential", function(e) {
            $("tbody", "#table-credentials").append(
                "<tr class='credential'><td colspan='3'>" +
                "<input type='text' placeholder='type username' maxlength='16' name='user' value='' />" +
                "<a href='#' class='save-new-credential btn btn-primary'>Save</a>" +
                "</td></tr>");
        });

        $(document).on("click.save-new-credential", ".save-new-credential", function(e) {
            var $insert_row = $(e.target).parent().parent(),
                username = $("input", $insert_row).val();

            CredentialManager.create(username, $insert_row, function(credential) {
                $insert_row.remove();

                // show password
                credential.show_password();
            });
            return false;
        });

        var show_error_message = function($row, message) {
            var errormsg = '<div class="alert alert-error"><button type="button" class="close" data-dismiss="alert">&times;</button><strong>Erro</strong> ' + message + '</div>';
            $('td .alert', $row).remove();
            $('td', $row).prepend(errormsg);
        };


        return {
            /**
            * Get the credential object with pk specified. If no credential exists
            * in page, returns null.
            */
            get: function(credential_pk) {
                var $row = $("#table-credentials tr[data-credential-pk=" + credential_pk + "]");
                if ($row.length === 0) {
                    return null;
                }
                return new Credential($row);
            },
            /**
            * Includes a credential json representation on page (always include at the bottom).
            */
            include: function(credential_json) {
                var selector = "#table-credentials tr[data-credential-pk=" + credential_json.credential.pk + "]";

                var html_row = $("#credential-template").mustache(credential_json);
                $("tbody", "#table-credentials").append(html_row);

                // request new DOM element already attached
                var credential = this.get(credential_json.credential.pk);
                initialize_listeners(credential);
                return credential;
            },
            /**
            * Create a new credential on server and put on page
            */
            create: function(username, $row, callback) {
                var self = this;
                $.ajax({
                    "url": "/logical/credential/",
                    "type": "POST",
                    "data": { "username": username, "database_id": get_database_id() },
                }).done(function(data) {
                    if (data.error) {
                        show_error_message($row, data.error);
                    }

                    var credential = self.include(data);
                    if (callback) {
                        callback(credential);
                    }
                }).fail(function() {
                    show_error_message($row, 'Invalid server response');
                });
            }
        };
    })();
    window.CredentialManager = CredentialManager;

    var Database = function() {
        this.update_components();
    };

    Database.prototype = {
        update_components: function() {
            this.filter_plans();
        },
        filter_plans: function() {
            var environment_id = $("#id_environment").val() || "none";
            var engine_id = $("#id_engine").val() || "none";
            var data_environment_attribute = "data-environment-" + environment_id;
            var data_engine_attribute = "data-engine-" + engine_id;
            $(".plan").each(function(index, el) {
                var $el = $(el);
                if ($el.attr(data_environment_attribute) && $el.attr(data_engine_attribute)) {
                    $(el).parent().show('fast');
                } else {
                    ($el).parent().hide('fast');
                }
            });
        }
    };

    // Document READY
    $(function() {
        var database = new Database();

        $("#id_environment, #id_engine").on("change", function() {
            database.update_components();
        });

        $(".plan").on("click", function() {
            $("input", ".plan").removeAttr("checked");
            $("input", $(this)).attr("checked", "checked");
        });

        $(".btn-plan").on("click", function(ev) {
            $("#plan-type").val(this.dataset.planId);
            $(".btn-plan").attr('disabled', true);
            confirmation = confirm('Are you sure?');

            if(confirmation== true){
                $("#database_form").submit();
            }else{
                $(".btn-plan").attr('disabled', false);
                return false
            }

        });

        $( document ).ready(function() {
            if ($("#id_offering").val() == ""){
                $("#id_offering").hide();
                $("#resizeDatabase").hide();
                $('input[id="id_offering"], label[for="id_offering"]').hide();

            }
        });

        var endpoint_popover_active = null;
        $('.show-endpoint').popover({'trigger': 'manual', 'html': false})
        .on('click', function(e) {
            var $this = $(this);
            if (endpoint_popover_active && endpoint_popover_active.attr('data-content') != $this.attr('data-content')) {
                endpoint_popover_active.popover('hide');
            }
            endpoint_popover_active = $this;
            endpoint_popover_active.popover('toggle');
            e.preventDefault();
        });
    });

})(django.jQuery);
